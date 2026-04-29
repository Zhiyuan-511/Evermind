"""
Evermind Backend — Plugin Implementations
Built-in plugins: screenshot, browser, source_fetch, file_ops, comfyui, image_gen, video_review, shell, git, computer_use, ui_control
"""

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from html_postprocess import materialize_local_runtime_assets, postprocess_generated_text
from preview_validation import inspect_html_integrity, is_bootstrap_html_artifact, validate_html_content

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw, ImageFont
except Exception:  # pragma: no cover - optional dependency
    PILImage = None
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore

from .base import Plugin, PluginResult, PluginRegistry, SecurityLevel

logger = logging.getLogger("evermind.plugins")


# V4.9.7 FIX: Store (token, timestamp) to enable TTL-based cleanup and prevent memory leaks.
_ACTIVE_FILE_OPS_WRITE_TOKENS: Dict[str, Tuple[str, float]] = {}
_FILE_OPS_WRITE_TOKEN_TTL_SEC = 3600  # 1 hour
_FILE_OPS_READ_HISTORY: Dict[str, set[str]] = {}
_SHARED_ROOT_ASSET_NAMES = {"styles.css", "app.js", "style.css", "main.css", "main.js", "script.js"}


def _cleanup_expired_write_tokens() -> None:
    """Remove write tokens older than TTL to prevent unbounded memory growth."""
    now = time.time()
    expired = [k for k, (_, ts) in _ACTIVE_FILE_OPS_WRITE_TOKENS.items()
               if now - ts > _FILE_OPS_WRITE_TOKEN_TTL_SEC]
    for k in expired:
        _ACTIVE_FILE_OPS_WRITE_TOKENS.pop(k, None)
        for rk in list(_FILE_OPS_READ_HISTORY.keys()):
            if rk == k or rk.startswith(f"{k}:"):
                _FILE_OPS_READ_HISTORY.pop(rk, None)


def set_active_file_ops_write_token(node_execution_id: str, token: str) -> None:
    node_execution_id = str(node_execution_id or "").strip()
    token = str(token or "").strip()
    if not node_execution_id or not token:
        return
    _cleanup_expired_write_tokens()
    for key in list(_FILE_OPS_READ_HISTORY.keys()):
        if key == node_execution_id or key.startswith(f"{node_execution_id}:"):
            _FILE_OPS_READ_HISTORY.pop(key, None)
    _ACTIVE_FILE_OPS_WRITE_TOKENS[node_execution_id] = (token, time.time())


def clear_active_file_ops_write_token(node_execution_id: str, token: Optional[str] = None) -> None:
    node_execution_id = str(node_execution_id or "").strip()
    token = str(token or "").strip()
    if not node_execution_id:
        return
    entry = _ACTIVE_FILE_OPS_WRITE_TOKENS.get(node_execution_id)
    if entry is None:
        return
    current_token = entry[0]
    if token and current_token != token:
        return
    _ACTIVE_FILE_OPS_WRITE_TOKENS.pop(node_execution_id, None)
    for key in list(_FILE_OPS_READ_HISTORY.keys()):
        if key == node_execution_id or key.startswith(f"{node_execution_id}:"):
            _FILE_OPS_READ_HISTORY.pop(key, None)


# ─────────────────────────────────────────────
# 1. Screenshot Plugin
# ─────────────────────────────────────────────
class ScreenshotPlugin(Plugin):
    name = "screenshot"
    display_name = "Screenshot"
    description = "Capture screenshots of the screen, a window, or a specific region"
    icon = "fa-camera"
    security_level = SecurityLevel.L1

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            import pyautogui
            from PIL import Image

            region = params.get("region")  # (x, y, w, h) or None for fullscreen
            if region:
                img = pyautogui.screenshot(region=tuple(region))
            else:
                img = pyautogui.screenshot()

            # Convert to base64 for transmission
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            # Also save to temp file
            output_dir = Path((context or {}).get("output_dir", "/tmp"))
            output_dir.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=str(output_dir))
            img.save(tmp.name)

            return PluginResult(
                success=True,
                data={"path": tmp.name, "width": img.width, "height": img.height},
                artifacts=[{"type": "image", "path": tmp.name, "base64": b64}]
            )
        except Exception as e:
            return PluginResult(success=False, error=str(e))

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Screenshot region [x, y, width, height]. Omit for fullscreen."
                }
            }
        }


# ─────────────────────────────────────────────
# 2. Browser Plugin
# ─────────────────────────────────────────────
class BrowserPlugin(Plugin):
    name = "browser"
    display_name = "Browser"
    description = (
        "Virtuoso-grade agentic web browser with vision grounding + pixel-precise mouse & keyboard control. "
        "Semantic actions: navigate, observe (AX-tree + DOM fused, paint-order filtered), act (click/fill/wait/press/press_sequence), "
        "snapshot, extract, scroll, record_scroll, find (off-viewport text search), hover, select (dropdown), upload, "
        "new_tab / switch_tab / close_tab, evaluate (custom JS), close_popups, network_idle, press, wait_for. "
        "Pixel-level primitives (for canvas games, WebGL, custom UIs): mouse_click (button + click_count + optional canvas origin), "
        "mouse_move, mouse_down/mouse_up (drag primitives), drag (full from→to), wheel (scroll at xy), "
        "key_down/key_up (separate keyboard events — WASD hold, game combos), key_hold (press & hold for N ms), "
        "type_text (per-char delay for anti-bot inputs), canvas_click (click local coords inside a specific <canvas>), "
        "screenshot_region (region crop). Batch: macro (run 3-20 sub-actions in one call with optional wait steps). "
        "Any turn that includes a snapshot gets an auto-annotated screenshot with numbered boxes matching each "
        "element's [index], so VLMs ground clicks visually."
    )
    icon = "fa-globe"
    security_level = SecurityLevel.L2

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._headless = True
        self._requested_headless = True
        self._launch_note = ""
        self._force_headless_session = False
        self._bound_page_identity = None
        self._console_errors: List[Dict[str, str]] = []
        self._page_errors: List[str] = []
        self._failed_requests: List[Dict[str, str]] = []
        self._action_log: List[Dict[str, Any]] = []
        self._last_state_hash: Optional[str] = None
        self._active_plugin_context: Dict[str, Any] = {}
        self._trace_active = False
        # v6.1.3 (Opus review #1-P2): explicit init so shutdown()→new launch
        # cycle always starts with focus_claimed_once=False (was set once and
        # never reset across shutdowns → second browser window silently stayed
        # behind Evermind).
        self._focus_claimed_once: bool = False

    def _resolve_headless(self, context: Dict[str, Any] | None = None) -> bool:
        if self._force_headless_session:
            return True
        # v7.11 (maintainer 2026-04-28): default to HEADLESS unconditionally for
        # better UX. User reported "reviewer 打开外部浏览器之前还是会先打开内部
        # 浏览器" — the second visible Chromium window was confusing. Reviewer
        # tools (screenshot/click/dom_snapshot) work identically headless.
        # Only opt in to headful when EVERMIND_BROWSER_HEADFUL=1 is set
        # explicitly OR context.browser_headful=True (orchestrator can flip
        # for specific debugging sessions).
        if isinstance(context, dict) and "browser_headful" in context:
            return not bool(context.get("browser_headful"))
        env_headful = str(os.getenv("EVERMIND_BROWSER_HEADFUL", "0")).strip().lower() in ("1", "true", "yes", "on")
        return not env_headful

    async def _create_context_and_page(self, *, headless: bool):
        self._context = await self._browser.new_context(viewport={"width": 1280, "height": 800})
        self._page = await self._context.new_page()
        self._headless = headless

    async def _relaunch_headless_after_visible_failure(self, launch_args: List[str], reason: Exception):
        logger.warning("BrowserPlugin forcing headless fallback after visible browser failure: %s", reason)
        self._force_headless_session = True
        await self.shutdown()
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True, args=launch_args)
        self._launch_note = f"forced headless fallback: {reason}"
        await self._create_context_and_page(headless=True)
        self._requested_headless = False

    async def _cdp_attach_if_available(self, headless: bool) -> bool:
        """v5.8.6: attach to Evermind's embedded Chromium via CDP.

        Electron sets `--remote-debugging-port=9222` (see electron/main.js). When
        the port is reachable, the reviewer/tester should drive the user's
        existing Chromium window instead of spawning a separate Playwright
        browser — the external Playwright window is visibly laggy and
        confusing to the user.
        """
        # v6.4.9 (maintainer 2026-04-22): CDP attach is OPT-IN only.
        # Previously the backend auto-attached to port 19222 whenever Electron
        # exported the URL — result: reviewer/tester/analyst all drove the
        # user's visible browser window, yanking the tab away from whatever
        # the user was manually inspecting. CDP attach now requires the
        # caller to explicitly opt in via EVERMIND_BROWSER_ATTACH_CDP=1
        # (set by electron/main.js only when EVERMIND_ENABLE_CDP=1 is passed
        # in). Default: the reviewer/tester spawns an independent Playwright
        # Chromium that never conflicts with the user's session.
        attach_flag = str(os.getenv("EVERMIND_BROWSER_ATTACH_CDP", "0")).strip().lower() in {"1", "true", "yes", "on"}
        cdp_url = str(os.getenv("EVERMIND_BROWSER_CDP_URL", "")).strip()
        if not attach_flag or not cdp_url:
            return False
        try:
            import socket
            from urllib.parse import urlparse
            parsed = urlparse(cdp_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 9222
            with socket.create_connection((host, port), timeout=0.4):
                pass
        except Exception as exc:
            logger.debug("CDP endpoint %s not reachable: %s", cdp_url, exc)
            return False
        try:
            from playwright.async_api import async_playwright
            if not self._playwright:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
            # v6.0 FIX: the original code reused pages[0] of the first context,
            # which in Electron is the MAIN UI page. Calling page.goto() on it
            # navigated the entire Evermind main window to the preview URL —
            # the user saw the Evermind UI get replaced by a fullscreen
            # browser. Always open a NEW page so the agent's browsing happens
            # in a separate window/tab that can be closed without killing the
            # app. Electron honours "new page" by opening a fresh BrowserWindow
            # via setWindowOpenHandler (now sized ~1300×860, movable).
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = await self._browser.new_context(viewport={"width": 1280, "height": 800})
            self._page = await self._context.new_page()
            self._headless = False  # Electron is headful from the user's perspective
            self._launch_note = f"attached to embedded Chromium via CDP {cdp_url}"
            logger.info(
                "BrowserPlugin attached to embedded Chromium via CDP (%s) — skipping Playwright launch",
                cdp_url,
            )
            return True
        except Exception as exc:
            logger.warning("CDP attach to %s failed, falling back to Playwright launch: %s", cdp_url, exc)
            self._browser = None
            self._context = None
            self._page = None
            return False

    async def _ensure_browser(self, context: Dict[str, Any] | None = None):
        self._active_plugin_context = dict(context or {})
        requested_headless = self._resolve_headless(context)
        headless = requested_headless
        # v6.4.14 (maintainer 2026-04-22): capture whichever app the user was
        # actively in RIGHT BEFORE we pop up a headful Chromium. After
        # each browser action we restore focus to that app (see
        # `_restore_user_focus_nonblocking`) so AI browser operations never
        # yank the user out of their editor / terminal / whatever.
        # Headless sessions don't steal focus, so skip the capture there.
        if not requested_headless and not getattr(self, "_pre_launch_frontmost_app", ""):
            self._capture_pre_launch_frontmost_app()
        # v6.0 FIX: when launching headful Playwright Chromium (reviewer /
        # tester drives a visible browser for gameplay QA), the default
        # Chromium window opens full-screen and completely covers the
        # Evermind app. Also it's borderless in some configs so the user
        # can't drag it. Set explicit small window + offset so:
        #   1. Evermind stays visible behind/beside the browser
        #   2. User can drag / resize normally
        #   3. Browser doesn't cover ⌘Q / task switcher affordances
        # v6.1.2: anti-lag flags. Playwright-launched Chromium on macOS can
        # feel laggy because of (a) Metal backend off by default, (b) GPU
        # raster disabled under some configs, (c) occlusion causing throttle
        # when Evermind main window overlaps it. These flags mirror what
        # browser-use / Cline / Anthropic computer-use use for headful.
        # v6.4.27 (maintainer 2026-04-22): do NOT open startup windows + keep
        # Chromium from auto-activating on macOS. Documented on Peter
        # Beverloo's Chromium switch list — these are the exact flags
        # used by chrome-devtools-mcp + Playwright community to silence
        # first-paint window activation.
        _show_explicit = str(os.getenv("EVERMIND_BROWSER_SHOW", "0")).strip().lower() in ("1", "true", "yes")
        launch_args = [
            "--silent-launch",
            "--no-startup-window",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion,BackForwardCache,CrossOriginOpenerPolicy,GlobalMediaControls,MediaRouter,DialMediaRouteProvider",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-dev-shm-usage",
            # v6.1.2 perf: Playwright Chromium on macOS runs ~5x slower than
            # stock Chrome (playwright#23914) because Metal GPU backend is
            # OFF by default. The ANGLE-Metal trio below is the single most
            # effective fix (michelkraemer.com benchmark: 1.9s → 1.3s).
            "--use-gl=angle",
            "--use-angle=metal",
            "--enable-gpu-rasterization",
            "--enable-zero-copy",
            "--ignore-gpu-blocklist",
            "--enable-smooth-scrolling",
            "--disable-ipc-flooding-protection",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-translate",
            # v7.1f (maintainer 2026-04-24): force no HTTP cache so reviewer/tester
            # always observes the LATEST patched code. Without this, after
            # patcher edits e.g. shared/nav.js, the reviewer re-navigates
            # with a `?t=` buster on index.html but the linked nav.js URL
            # is unchanged → browser serves cached old version → reviewer
            # audits stale code.
            "--disable-application-cache",
            "--disable-cache",
            "--disable-offline-load-stale-cache",
            "--disk-cache-size=0",
            "--media-cache-size=0",
        ]
        # Window position: offscreen when silent (default reviewer/tester),
        # side-by-side only when user explicitly opts in to watch.
        if _show_explicit:
            launch_args.extend([
                "--window-size=1280,820",
                "--window-position=760,80",
            ])
        else:
            # Park far offscreen so even if macOS decides to show it, user
            # doesn't see flicker. Tiny size further reduces GPU cost.
            launch_args.extend([
                "--window-size=100,100",
                "--window-position=-2400,-2400",
            ])

        # Recreate browser when mode switches between headless/headful.
        if self._browser and self._headless != headless:
            await self.shutdown()

        # v6.1.2: DISABLE CDP attach by default. Electron Chromium's CDP subset
        # rejects `Target.createTarget` (required by Playwright new_page), and
        # the failure path can leave a dangling CDP connection that makes the
        # Evermind main window flicker or navigate unexpectedly for ~10s
        # before Playwright falls back to a fresh Chromium. The "internal
        # browser" experience wasn't actually internal — users saw:
        #   1. Evermind main window glitch for 10s (CDP hanging)
        #   2. An external Chromium window pop up (Playwright fallback)
        # Turning CDP attach OFF: Playwright headful is launched directly
        # with Evermind-branded window-position/size so it LOOKS like a
        # first-party tool pane. Overlay (_ensure_cursor_overlay) injects
        # reliably into the Playwright page. Set EVERMIND_BROWSER_ENABLE_CDP=1
        # to re-enable the old attempt for debugging.
        cdp_explicitly_enabled = str(os.getenv("EVERMIND_BROWSER_ENABLE_CDP", "0")).strip().lower() in ("1", "true", "yes")
        cdp_disabled = str(os.getenv("EVERMIND_BROWSER_DISABLE_CDP", "0")).strip().lower() in ("1", "true", "yes") or (not cdp_explicitly_enabled)
        cdp_attach_tried = False
        cdp_attach_ok = False
        if not self._browser and not cdp_disabled and not headless:
            cdp_attach_tried = True
            cdp_attach_ok = await self._cdp_attach_if_available(headless)
            if cdp_attach_ok:
                self._bind_page_diagnostics(self._page)
                self._requested_headless = False
                return self._page

        # v6.1.2 (refined): when CDP attach to the embedded Electron Chromium
        # fails, decide whether to still pop a visible Playwright window based
        # on caller intent. tester/reviewer explicitly ask for a visible
        # window via context["browser_headful"]=True — those ARE the nodes the
        # user wants to watch (gameplay QA, visual diff). Nodes that did NOT
        # opt in silently go headless so we don't surprise the user with a
        # rogue Chromium pop-up (the original analyst bug).
        if cdp_attach_tried and not cdp_attach_ok and not headless:
            force_visible = str(os.getenv("EVERMIND_BROWSER_FORCE_VISIBLE", "0")).strip().lower() in ("1", "true", "yes")
            ctx_map = context if isinstance(context, dict) else {}
            caller_wants_visible = bool(
                ctx_map.get("visible")
                or ctx_map.get("browser_headful")
                or ctx_map.get("force_visible")
            )
            # Also infer from node_type if provided.
            _node_role = str(ctx_map.get("node_type") or ctx_map.get("node") or "").strip().lower()
            # v6.4.27 (maintainer 2026-04-22): reviewer/tester NO LONGER default to
            # visible. They run headless unless the user opts in via
            # EVERMIND_BROWSER_SHOW=1 env var. Reason: users reported the
            # reviewer pulling focus away every ~15s during multi-round browser
            # interactions, and the Playwright-fallback visible window appearing
            # BEFORE the first tool call — both resolved by headless-by-default.
            # Chat/uidesign still default visible because the user is actively
            # WATCHING the AI drive the page (the integration point).
            _show_all = str(os.getenv("EVERMIND_BROWSER_SHOW", "0")).strip().lower() in ("1", "true", "yes")
            if _node_role in {"uidesign", "chat"}:
                caller_wants_visible = True
            elif _node_role in {"tester", "reviewer"} and _show_all:
                caller_wants_visible = True
            # else: reviewer/tester without opt-in → stays headless
            if not (force_visible or caller_wants_visible):
                logger.info(
                    "CDP attach failed; node did not request visible — using "
                    "HEADLESS Playwright to avoid surprise window. "
                    "(tester/reviewer pass browser_headful=True to keep visible.)"
                )
                headless = True
                self._force_headless_session = True
            else:
                logger.info(
                    "CDP attach failed but caller requested visible window — "
                    "launching headful Playwright so the user can watch (role=%s).",
                    _node_role or "unspecified",
                )

        if not self._browser:
            from playwright.async_api import async_playwright
            if not self._playwright:
                self._playwright = await async_playwright().start()
            # v6.1.2 perf: prefer a real installed Chrome Stable (~5x faster
            # than Playwright's bundled Chromium on macOS per playwright#23914).
            # Only applies on macOS where /Applications/Google Chrome.app is a
            # common install. Falls back to bundled Chromium if unavailable.
            _use_stable_chrome = str(os.getenv("EVERMIND_BROWSER_USE_STABLE_CHROME", "auto")).strip().lower()
            _launch_kwargs: Dict[str, Any] = {"headless": headless, "args": launch_args}
            if _use_stable_chrome != "never" and not headless:
                try:
                    _stable_chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                    if os.path.exists(_stable_chrome_path):
                        _launch_kwargs["channel"] = "chrome"
                        logger.info("BrowserPlugin: using installed Chrome Stable for %sx perf win over bundled Chromium", "~5")
                except Exception:
                    pass
            elif headless:
                logger.debug(
                    "[v7.19c] BrowserPlugin: headless=True — skipping Chrome Stable channel to avoid macOS GUI window flash; using bundled Chromium."
                )
            try:
                self._browser = await self._playwright.chromium.launch(**_launch_kwargs)
                self._launch_note = ""
            except Exception as launch_err:
                if headless:
                    raise
                # Fall back so workflow can continue even if GUI launch is blocked.
                logger.warning("BrowserPlugin headful launch failed; falling back to headless: %s", launch_err)
                self._force_headless_session = True
                self._browser = await self._playwright.chromium.launch(headless=True, args=launch_args)
                headless = True
                self._launch_note = f"requested headful, fallback to headless: {launch_err}"
            try:
                await self._create_context_and_page(headless=headless)
            except Exception as context_err:
                if headless or requested_headless:
                    raise
                await self._relaunch_headless_after_visible_failure(launch_args, context_err)
            self._requested_headless = requested_headless
        elif self._page is None or self._page.is_closed():
            try:
                self._page = await self._context.new_page()
            except Exception:
                # P0 FIX 2026-04-04: Browser/context may be closed between checks.
                # TargetClosedError is a common race here — recreate from scratch.
                await self.shutdown()
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                try:
                    self._browser = await self._playwright.chromium.launch(headless=headless, args=launch_args)
                except Exception:
                    self._force_headless_session = True
                    self._browser = await self._playwright.chromium.launch(headless=True, args=launch_args)
                    headless = True
                await self._create_context_and_page(headless=headless)
        # v6.4.27/28 (maintainer 2026-04-22): bring_to_front() is the single
        # biggest source of macOS focus-steal. For tester/reviewer (silent
        # QA) we NEVER call it — the window is parked offscreen anyway
        # and users don't need to see it. For chat/uidesign the user
        # actively watches the AI drive the page, so we DO call bring_to_front
        # exactly ONCE on the very first launch; subsequent tool calls
        # never touch activation. Still fires no osascript "activate
        # Evermind" — that was moving the steal from Chromium to Evermind
        # (same problem, just different target).
        _ctx_map = context if isinstance(context, dict) else {}
        _role_for_focus = str(_ctx_map.get("node_type") or _ctx_map.get("node") or "").strip().lower()
        _watch_role = _role_for_focus in {"chat", "uidesign"}
        if (
            _watch_role
            and not self._headless
            and not getattr(self, "_focus_claimed_once", False)
        ):
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
        # Set the flag unconditionally so we never call bring_to_front again
        # for this session, regardless of role. Second-and-onward tool calls
        # run silently.
        self._focus_claimed_once = True
        self._bind_page_diagnostics(self._page)
        return self._page

    async def _record_preview_session(self, params: Dict[str, Any], context: Dict[str, Any]) -> PluginResult:
        """v6.2 (maintainer 2026-04-20): record a short gameplay/preview clip for
        video-based review. Uses a dedicated Playwright context so the main
        browser session (CDP-attached or otherwise) is untouched.

        Params:
          url (required): preview URL or file:// to open
          task_type: 'game' | 'website' | 'slides' | other (drives interactions)
          duration_sec: soft cap (games: 25, websites: 10, slides: 5)
          save_dir: optional output directory; default /tmp/evermind_video_review

        Returns PluginResult.data = {
          video_path, file_size_bytes, duration_sec, task_type, console_errors
        }
        """
        url = str(params.get("url") or "").strip()
        if not url:
            return PluginResult(success=False, error="record_preview requires a url")

        task_type = str(params.get("task_type") or "game").strip().lower()
        save_dir_raw = str(params.get("save_dir") or "/tmp/evermind_video_review").strip()
        if task_type == "game":
            default_duration = 25
        elif task_type in ("website", "landing", "portfolio"):
            default_duration = 10
        elif task_type in ("slides", "presentation"):
            default_duration = 5
        else:
            default_duration = 10
        try:
            duration_sec = max(3, min(35, int(params.get("duration_sec", default_duration) or default_duration)))
        except (TypeError, ValueError):
            duration_sec = default_duration

        # Dedicated output dir (will be rmtree'd by caller after VideoReview)
        out_dir = Path(save_dir_raw)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return PluginResult(success=False, error=f"Cannot create save_dir: {exc}")

        # Import playwright separately — do NOT touch self._playwright/_browser.
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            return PluginResult(success=False, error=f"Playwright unavailable: {exc}")

        console_errors: List[Dict[str, str]] = []
        page_errors: List[str] = []
        video_path_final: Optional[str] = None
        started_ts = time.time()

        pw = None
        browser = None
        rec_context = None
        page = None
        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True, args=[
                "--disable-features=CalculateNativeWinOcclusion,BackForwardCache",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
                "--no-first-run", "--no-default-browser-check",
            ])
            rec_context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                record_video_dir=str(out_dir),
                record_video_size={"width": 640, "height": 360},  # cap size for VL models
            )
            page = await rec_context.new_page()

            page.on("console", lambda msg: (
                console_errors.append({"type": msg.type, "text": msg.text[:200]})
                if msg.type in ("error", "warning") else None
            ))
            page.on("pageerror", lambda err: page_errors.append(str(err)[:400]))

            await page.goto(url, wait_until="domcontentloaded", timeout=20000)

            # Per-task-type interaction scripts (best-effort; failures do not
            # abort recording — we still want the webm)
            try:
                if task_type == "game":
                    # Simulate gameplay inputs spread across duration window
                    await page.wait_for_timeout(1500)
                    canvas = await page.query_selector("canvas")
                    if canvas:
                        bbox = await canvas.bounding_box()
                        if bbox:
                            await page.mouse.click(bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
                    for key in ["KeyW", "KeyW", "KeyA", "KeyS", "KeyD", "Space"]:
                        try:
                            await page.keyboard.down(key)
                            await page.wait_for_timeout(500)
                            await page.keyboard.up(key)
                        except Exception:
                            pass
                    await page.wait_for_timeout(max(0, (duration_sec - 6) * 1000))
                elif task_type in ("website", "landing", "portfolio"):
                    await page.wait_for_timeout(1000)
                    for _ in range(4):
                        await page.mouse.wheel(0, 400)
                        await page.wait_for_timeout(700)
                    await page.wait_for_timeout(max(0, (duration_sec - 5) * 1000))
                elif task_type in ("slides", "presentation"):
                    await page.wait_for_timeout(800)
                    for _ in range(3):
                        try:
                            await page.keyboard.press("ArrowRight")
                        except Exception:
                            pass
                        await page.wait_for_timeout(700)
                    await page.wait_for_timeout(max(0, (duration_sec - 3) * 1000))
                else:
                    await page.wait_for_timeout(duration_sec * 1000)
            except Exception as exc:
                logger.info("record_preview interactions partial: %s", str(exc)[:120])

            # Resolve video path BEFORE closing (preferred: save_as after close)
            try:
                video_obj = page.video
            except Exception:
                video_obj = None

            # Close page first so recording finalizes, then context flushes webm
            try:
                await page.close()
            except Exception:
                pass
            try:
                await rec_context.close()
            except Exception:
                pass

            # After context close, either video.path() points at the flushed webm,
            # or we scan out_dir for the newest webm.
            candidate: Optional[Path] = None
            if video_obj:
                try:
                    candidate_raw = await video_obj.path()
                    if candidate_raw:
                        candidate = Path(candidate_raw)
                except Exception:
                    candidate = None

            if candidate is None or not candidate.exists():
                # Fallback: newest .webm in out_dir
                webms = sorted(out_dir.glob("*.webm"), key=lambda p: p.stat().st_mtime, reverse=True)
                if webms:
                    candidate = webms[0]

            if candidate and candidate.exists() and candidate.stat().st_size > 1024:
                video_path_final = str(candidate)
            else:
                return PluginResult(success=False, error="Recording produced no/empty webm",
                                    data={"console_errors": console_errors[:10]})

            return PluginResult(success=True, data={
                "video_path": video_path_final,
                "file_size_bytes": int(Path(video_path_final).stat().st_size),
                "duration_sec": round(time.time() - started_ts, 2),
                "task_type": task_type,
                "console_errors": console_errors[:10],
                "page_errors": page_errors[:5],
            }, artifacts=[{"type": "video", "path": video_path_final}])
        except Exception as exc:
            return PluginResult(success=False, error=f"record_preview failed: {str(exc)[:400]}")
        finally:
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                if pw:
                    await pw.stop()
            except Exception:
                pass

    async def shutdown(self):
        """Clean up browser resources.

        v5.8.6 CRITICAL FIX: when attached via CDP, DROP references only, do
        NOT call close(). Playwright's Browser.close() on a CDP-attached
        browser still tears down the Playwright-managed context/pages, which
        the user experiences as "the embedded browser window closes after
        ~10 seconds" because each reviewer tool call ended with shutdown().
        The page/context belong to Electron's Chromium, not to us.
        """
        attached_via_cdp = "attached to embedded Chromium via CDP" in (self._launch_note or "")
        try:
            if attached_via_cdp:
                # Drop refs only. The Playwright connection will be GC'd.
                # Do NOT stop playwright either — subsequent plugin calls in
                # the same node session need to re-attach via the same
                # async_playwright runtime. _ensure_browser will re-init on
                # demand.
                pass
            else:
                if self._page and not self._page.is_closed():
                    await self._page.close()
                if self._context:
                    await self._context.close()
                if self._browser:
                    await self._browser.close()
                if self._playwright:
                    await self._playwright.stop()
        except Exception as e:
            logger.warning(f"BrowserPlugin shutdown error: {e}")
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._headless = True
            self._requested_headless = True
            self._launch_note = ""
            self._bound_page_identity = None
            self._active_plugin_context = {}
            # v6.4.14 (maintainer 2026-04-22): clear the pre-launch frontmost-app
            # capture so the next session re-captures the user's then-current
            # app (they might switch between reviewer and tester runs).
            self._pre_launch_frontmost_app = ""
            self._trace_active = False
            # v6.1.3 (Opus review #1-P2): reset the one-shot focus flag so the
            # NEXT browser launch will also bring-to-front + activate Evermind
            # back. Without this the second/third/... browser session in the
            # same process silently opens behind Evermind.
            self._focus_claimed_once = False
            self._clear_diagnostics()

    def _clear_diagnostics(self):
        self._console_errors = []
        self._page_errors = []
        self._failed_requests = []
        self._action_log = []
        self._last_state_hash = None

    def _bind_page_diagnostics(self, page):
        page_identity = id(page)
        if self._bound_page_identity == page_identity:
            return
        self._bound_page_identity = page_identity
        self._clear_diagnostics()

        def _should_ignore_failed_request(url: str, error_text: str = "") -> bool:
            parsed = urlparse(url or "")
            host = str(parsed.hostname or "").strip().lower()
            path = str(parsed.path or "").strip().lower()
            if host in {
                "fonts.googleapis.com",
                "fonts.gstatic.com",
                "use.typekit.net",
                "fonts.bunny.net",
                "fast.fonts.net",
            }:
                return True
            if path.endswith((".woff", ".woff2", ".ttf", ".otf", ".eot")):
                return True
            return False

        def _on_console(msg):
            try:
                msg_type = str(msg.type or "").strip().lower()
                if msg_type != "error":
                    return
                text = str(msg.text or "").strip()
                if not text:
                    return
                self._console_errors.append({
                    "type": msg_type,
                    "text": text[:500],
                })
                self._console_errors = self._console_errors[-20:]
            except Exception:
                pass

        def _on_page_error(exc):
            try:
                text = str(exc or "").strip()
                if not text:
                    return
                self._page_errors.append(text[:500])
                self._page_errors = self._page_errors[-20:]
            except Exception:
                pass

        def _on_request_failed(request):
            try:
                url = str(request.url or "").strip()
                if not url or url.endswith("/favicon.ico") or url.startswith("data:"):
                    return
                failure = request.failure
                error_text = ""
                if failure is not None:
                    error_text = str(getattr(failure, "error_text", "") or getattr(failure, "error", "") or "").strip()
                resource_type = str(getattr(request, "resource_type", "") or "").strip().lower()
                if _should_ignore_failed_request(url, error_text):
                    return
                self._failed_requests.append({
                    "url": url[:300],
                    "error": error_text[:300],
                    "resource_type": resource_type[:40],
                })
                self._failed_requests = self._failed_requests[-20:]
            except Exception:
                pass

        page.on("console", _on_console)
        page.on("pageerror", _on_page_error)
        page.on("requestfailed", _on_request_failed)

    def _diagnostics_summary(self) -> Dict[str, Any]:
        return {
            "console_error_count": len(self._console_errors),
            "page_error_count": len(self._page_errors),
            "failed_request_count": len(self._failed_requests),
            "recent_console_errors": [dict(item) for item in self._console_errors[-3:]],
            "recent_page_errors": list(self._page_errors[-3:]),
            "recent_failed_requests": [dict(item) for item in self._failed_requests[-3:]],
            "recent_actions": [dict(item) for item in self._action_log[-8:]],
        }

    async def _safe_count(self, locator) -> int:
        try:
            return int(await locator.count())
        except Exception:
            return 0

    def _normalize_match_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    def _infer_act_subaction(self, params: Dict[str, Any]) -> str:
        explicit = str(
            params.get("subaction")
            or params.get("intent")
            or params.get("mode")
            or ""
        ).strip().lower()
        synonym_map = {
            "type": "fill",
            "input": "fill",
            "search": "fill",
            "click": "click",
            "tap": "click",
            "select": "click",
            "open": "click",
            "fill": "fill",
            "press": "press",
            "press_sequence": "press_sequence",
            "keyboard": "press_sequence",
            "wait": "wait_for",
            "wait_for": "wait_for",
        }
        if explicit in synonym_map:
            return synonym_map[explicit]
        if explicit in {"click", "fill", "press", "press_sequence", "wait_for"}:
            return explicit
        if params.get("keys"):
            return "press_sequence"
        if params.get("key"):
            return "press"
        if params.get("value") is not None:
            return "fill"
        if params.get("url_contains") or params.get("load_state") or params.get("state"):
            return "wait_for"
        return "click"

    def _score_snapshot_item(self, target: str, item: Dict[str, Any], *, intent: str) -> int:
        target_text = self._normalize_match_text(target)
        if not target_text:
            return -1
        ref = self._normalize_match_text(item.get("ref", ""))
        if target_text == ref:
            return 100

        combined = " ".join(
            self._normalize_match_text(item.get(key, ""))
            for key in ("text", "label", "placeholder", "id", "name", "selector", "role", "tag")
        ).strip()
        if not combined:
            return -1

        score = 0
        if target_text == self._normalize_match_text(item.get("text", "")):
            score += 40
        if target_text == self._normalize_match_text(item.get("label", "")):
            score += 36
        if target_text == self._normalize_match_text(item.get("placeholder", "")):
            score += 28
        if target_text in combined:
            score += 24

        target_tokens = [token for token in re.split(r"[^a-z0-9]+", target_text) if len(token) >= 2]
        score += sum(6 for token in target_tokens if token in combined)

        tag = self._normalize_match_text(item.get("tag", ""))
        role = self._normalize_match_text(item.get("role", ""))
        if intent == "fill":
            if tag in {"input", "textarea", "select"} or role in {"textbox", "searchbox", "combobox"}:
                score += 12
            else:
                score -= 4
        elif intent == "click":
            if tag in {"button", "a", "summary"} or role in {"button", "link", "tab"}:
                score += 10
        elif intent in {"press", "press_sequence"} and (tag in {"input", "textarea", "canvas"} or role in {"textbox", "searchbox"}):
            score += 6
        return score

    async def _resolve_target_hint(self, page, target: str, *, intent: str) -> Tuple[Optional[Any], str, Optional[Dict[str, Any]]]:
        snapshot = await self._page_snapshot(page, limit=80)
        interactive = snapshot.get("interactive") if isinstance(snapshot, dict) else None
        if not isinstance(interactive, list):
            return None, "", None
        ranked = sorted(
            (
                (self._score_snapshot_item(target, item or {}, intent=intent), item or {})
                for item in interactive
                if isinstance(item, dict)
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best_item = ranked[0] if ranked else (-1, None)
        if best_score <= 0 or not isinstance(best_item, dict):
            return None, "", None
        ref = str(best_item.get("ref", "") or "").strip()
        locator = None
        locator_target = ""
        if ref:
            locator, locator_target = await self._resolve_snapshot_ref(page, ref)
        if locator is None:
            return None, "", None
        label = str(best_item.get("text") or best_item.get("label") or best_item.get("placeholder") or ref).strip()
        role = str(best_item.get("role") or best_item.get("tag") or "").strip()
        descriptor = f"{target} -> {ref}"
        if label:
            descriptor += f" {label[:72]}"
        if role:
            descriptor += f" [{role[:24]}]"
        if locator_target:
            descriptor += f" via {locator_target}"
        return locator, descriptor, best_item

    def _summarize_snapshot(self, snapshot: Dict[str, Any], goal: str = "") -> str:
        if not isinstance(snapshot, dict):
            return ""
        counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {}
        title = str(snapshot.get("title", "") or "").strip()
        body_text = self._normalize_match_text(snapshot.get("body_text", ""))[:260]
        refs = self._snapshot_ref_preview(snapshot, limit=6)
        ref_text = ", ".join(
            " ".join(
                bit for bit in (
                    str(item.get("ref", "")).strip(),
                    str(item.get("label", "")).strip()[:40],
                    f"[{str(item.get('role', '')).strip()}]" if str(item.get("role", "")).strip() else "",
                )
                if bit
            )
            for item in refs[:4]
        )
        parts: List[str] = []
        if goal:
            parts.append(f"Focus: {str(goal).strip()[:120]}")
        if title:
            parts.append(f"Page: {title[:120]}")
        if counts:
            parts.append(
                "Visible controls: "
                f"{int(counts.get('buttons', 0) or 0)} buttons, "
                f"{int(counts.get('links', 0) or 0)} links, "
                f"{int(counts.get('inputs', 0) or 0)} inputs, "
                f"{int(counts.get('forms', 0) or 0)} forms, "
                f"{int(counts.get('canvas', 0) or 0)} canvas"
            )
        if ref_text:
            parts.append(f"Best refs: {ref_text}")
        if body_text:
            parts.append(f"Body summary: {body_text}")
        return " | ".join(parts)[:900]

    async def _resolve_snapshot_ref(self, page, ref: str, *, exact: bool = False) -> Tuple[Optional[Any], str]:
        snapshot = await self._page_snapshot(page, limit=80)
        interactive = snapshot.get("interactive") if isinstance(snapshot, dict) else None
        if not isinstance(interactive, list):
            return None, f"ref={ref}"
        target_item = None
        for item in interactive:
            if str((item or {}).get("ref", "") or "").strip() == ref:
                target_item = item or {}
                break
        if not isinstance(target_item, dict):
            return None, f"ref={ref}"

        candidates: List[Tuple[Any, str]] = []
        selector = str(target_item.get("selector", "") or "").strip()
        label = str(target_item.get("label", "") or "").strip()
        placeholder = str(target_item.get("placeholder", "") or "").strip()
        role = str(target_item.get("role", "") or "").strip()
        text = str(target_item.get("text", "") or "").strip()

        if selector:
            candidates.append((page.locator(selector), selector))
        if label:
            candidates.append((page.get_by_label(label, exact=exact), f"label={label}"))
        if placeholder:
            candidates.append((page.get_by_placeholder(placeholder, exact=exact), f"placeholder={placeholder}"))
        if text and role:
            candidates.append((page.get_by_role(role, name=text, exact=exact), f"role={role} name={text}"))
        if text:
            candidates.append((page.get_by_text(text, exact=exact), f"text={text}"))
        if role:
            candidates.append((page.get_by_role(role), f"role={role}"))

        for locator, locator_target in candidates:
            if await self._safe_count(locator) > 0:
                return locator.nth(0), f"ref={ref} -> {locator_target}"
        return None, f"ref={ref}"

    async def _resolve_locator(self, page, params: Dict[str, Any]) -> Tuple[Optional[Any], str]:
        ref = str(params.get("ref", "") or "").strip()
        selector = str(params.get("selector", "") or "").strip()
        text = str(params.get("text", "") or "").strip()
        role = str(params.get("role", "") or "").strip()
        label = str(params.get("label", "") or "").strip()
        placeholder = str(params.get("placeholder", "") or "").strip()
        exact = bool(params.get("exact", False))
        nth = params.get("nth")
        locator = None
        target = ""

        if ref:
            locator, target = await self._resolve_snapshot_ref(page, ref, exact=exact)
        if locator is None and selector:
            locator = page.locator(selector)
            target = selector
        elif locator is None and label:
            locator = page.get_by_label(label, exact=exact)
            target = f"label={label}"
        elif locator is None and placeholder:
            locator = page.get_by_placeholder(placeholder, exact=exact)
            target = f"placeholder={placeholder}"
        elif locator is None and text and role:
            locator = page.get_by_role(role, name=text, exact=exact)
            target = f"role={role} name={text}"
        elif locator is None and text:
            locator = page.get_by_text(text, exact=exact)
            target = f"text={text}"
        elif locator is None and role:
            locator = page.get_by_role(role)
            target = f"role={role}"

        if locator is None:
            return None, ""

        count = await self._safe_count(locator)
        if count <= 0:
            return None, target

        index = 0
        if nth is not None:
            try:
                index = max(0, min(count - 1, int(nth)))
            except Exception:
                index = 0
        return locator.nth(index), target

    async def _page_snapshot(self, page, limit: int = 30) -> Dict[str, Any]:
        # v5.8.4: fused DOM + accessibility + paint-order snapshot.
        # Matches browser-use's interactive index model:
        # - visibility + viewport + size gating
        # - paint-order occlusion check (elementFromPoint at center)
        # - bbox deduplication (drop child if parent covers ~same bounds)
        # - AX role/name via computed aria semantics (inline; page.accessibility
        #   is expensive and repeats the whole tree — we just read the attrs we care about)
        # - bbox coordinates kept on every item so downstream can annotate screenshots
        script = """
(LIMIT) => {
  const normalize = (value, maxLen = 160) => String(value == null ? '' : value).replace(/\\s+/g, ' ').trim().slice(0, maxLen);
  const vw = Math.max(0, window.innerWidth || document.documentElement.clientWidth || 0);
  const vh = Math.max(0, window.innerHeight || document.documentElement.clientHeight || 0);
  const isVisible = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    if (rect.bottom < -50 || rect.top > vh + 50) return false;
    if (rect.right < -50 || rect.left > vw + 50) return false;
    return true;
  };
  const isObstructed = (el, rect) => {
    try {
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      if (cx < 0 || cy < 0 || cx > vw || cy > vh) return false;
      const top = document.elementFromPoint(cx, cy);
      if (!top || top === el) return false;
      if (el.contains(top) || top.contains(el)) return false;
      return true;
    } catch (e) { return false; }
  };
  const computeRole = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.trim();
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (['button','submit','reset'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      return 'textbox';
    }
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'summary') return 'button';
    return '';
  };
  const computeName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const ids = labelledBy.split(/\\s+/).filter(Boolean);
      const parts = ids.map((id) => {
        const node = document.getElementById(id);
        return node ? (node.innerText || node.textContent || '') : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    if (el.tagName === 'INPUT' && el.id) {
      const lbl = document.querySelector('label[for="' + el.id + '"]');
      if (lbl) return lbl.innerText || lbl.textContent || '';
    }
    return el.innerText || el.textContent || el.value || el.getAttribute('title') || el.getAttribute('placeholder') || '';
  };
  const cssPath = (el) => {
    if (!el) return '';
    if (el.id) return '#' + CSS.escape(el.id);
    const dataTestId = el.getAttribute('data-testid');
    if (dataTestId) return '[data-testid="' + dataTestId.replace(/"/g,'\\\\"') + '"]';
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 4) {
      let part = node.tagName.toLowerCase();
      const nameAttr = node.getAttribute('name');
      if (nameAttr) {
        part += '[name="' + nameAttr.replace(/"/g,'\\\\"') + '"]';
        parts.unshift(part);
        break;
      }
      const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((child) => child.tagName === node.tagName) : [];
      if (siblings.length > 1) {
        part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };
  const rawQuery = 'button, a[href], input, textarea, select, summary, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], [role="combobox"], [role="switch"], [onclick], [data-testid], [tabindex]:not([tabindex="-1"])';
  const raw = Array.from(document.querySelectorAll(rawQuery)).filter(isVisible);
  // Bbox deduplication: if a parent+child share ~same bounds, drop the child (parent wins).
  const keep = new Array(raw.length).fill(true);
  for (let i = 0; i < raw.length; i++) {
    if (!keep[i]) continue;
    const a = raw[i].getBoundingClientRect();
    const aArea = a.width * a.height;
    for (let j = 0; j < raw.length; j++) {
      if (i === j || !keep[j]) continue;
      if (!raw[i].contains(raw[j])) continue;
      const b = raw[j].getBoundingClientRect();
      const bArea = b.width * b.height;
      if (bArea / Math.max(1, aArea) > 0.95) keep[j] = false;
    }
  }
  const filtered = raw.filter((_, i) => keep[i]);
  // Paint-order: keep elements whose center point resolves to themselves or a descendant.
  const paintOk = filtered.filter((el) => !isObstructed(el, el.getBoundingClientRect()));
  const items = paintOk.slice(0, LIMIT).map((el, idx) => {
    const rect = el.getBoundingClientRect();
    return {
      ref: 'ref-' + (idx + 1),
      index: idx + 1,
      tag: el.tagName.toLowerCase(),
      role: normalize(computeRole(el), 40),
      text: normalize(computeName(el), 160),
      id: normalize(el.id, 80),
      name: normalize(el.getAttribute('name'), 80),
      label: normalize(el.getAttribute('aria-label'), 80),
      placeholder: normalize(el.getAttribute('placeholder'), 80),
      type: normalize(el.getAttribute('type'), 40),
      selector: normalize(cssPath(el), 220),
      bbox: {
        x: Math.round(rect.left),
        y: Math.round(rect.top),
        w: Math.round(rect.width),
        h: Math.round(rect.height),
      },
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    };
  });
  // Scrollable regions hint — useful for SPAs with infinite scroll.
  const scrollables = [];
  Array.from(document.querySelectorAll('main, section, div, article, aside, nav')).forEach((el) => {
    if (scrollables.length >= 6) return;
    const s = window.getComputedStyle(el);
    if (['auto','scroll','overlay'].includes(s.overflowY) && el.scrollHeight - el.clientHeight > 40) {
      scrollables.push(normalize(cssPath(el), 220));
    }
  });
  const bodyText = normalize(document.body ? document.body.innerText : '', 2000);
  return {
    title: document.title || '',
    url: location.href,
    body_text: bodyText,
    interactive: items,
    scrollables: scrollables,
    viewport: { w: vw, h: vh },
    counts: {
      buttons: document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]').length,
      links: document.querySelectorAll('a[href], [role="link"]').length,
      forms: document.forms.length,
      inputs: document.querySelectorAll('input, textarea, select').length,
      canvas: document.querySelectorAll('canvas').length,
      iframes: document.querySelectorAll('iframe').length,
      occluded_dropped: filtered.length - paintOk.length,
      bbox_dropped: raw.length - filtered.length,
    }
  };
}
        """
        try:
            snapshot = await page.evaluate(script, max(1, min(limit, 80)))
            if isinstance(snapshot, dict):
                return snapshot
        except Exception as exc:
            return {"error": str(exc)[:300]}
        return {}

    def _snapshot_ref_preview(self, snapshot: Dict[str, Any], limit: int = 8) -> List[Dict[str, str]]:
        interactive = snapshot.get("interactive") if isinstance(snapshot, dict) else None
        if not isinstance(interactive, list):
            return []
        preview: List[Dict[str, str]] = []
        for item in interactive[:max(1, min(limit, 12))]:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref", "") or "").strip()
            if not ref:
                continue
            label = ""
            for key in ("text", "label", "placeholder", "id", "name", "selector"):
                candidate = str(item.get(key, "") or "").strip()
                if candidate:
                    label = candidate[:90]
                    break
            role = str(item.get("role", "") or item.get("tag", "") or "").strip()[:40]
            preview.append({
                "ref": ref,
                "label": label,
                "role": role,
            })
        return preview

    async def _get_scroll_metrics(self, page) -> Dict[str, Any]:
        try:
            metrics = await page.evaluate(
                """() => {
                    const root = document.documentElement || {};
                    const body = document.body || {};
                    const scrollY = Number(window.scrollY || window.pageYOffset || root.scrollTop || body.scrollTop || 0);
                    const viewportHeight = Number(window.innerHeight || root.clientHeight || 0);
                    const pageHeight = Number(Math.max(
                        body.scrollHeight || 0,
                        root.scrollHeight || 0,
                        body.offsetHeight || 0,
                        root.offsetHeight || 0,
                        body.clientHeight || 0,
                        root.clientHeight || 0,
                    ));
                    return {scrollY, viewportHeight, pageHeight};
                }"""
            )
            if isinstance(metrics, dict):
                return metrics
        except Exception:
            pass
        return {}

    def _scroll_metadata(
        self,
        metrics: Dict[str, Any],
        *,
        direction: str = "down",
        previous_scroll_y: Optional[int] = None,
    ) -> Dict[str, Any]:
        scroll_y = int((metrics or {}).get("scrollY", 0) or 0)
        viewport_height = int((metrics or {}).get("viewportHeight", 0) or 0)
        page_height = int((metrics or {}).get("pageHeight", 0) or 0)
        max_scroll = max(page_height - viewport_height, 0)
        at_bottom = bool(direction == "down" and scroll_y >= max(max_scroll - 4, 0))
        at_top = bool(direction != "down" and scroll_y <= 4)
        return {
            "scroll_y": scroll_y,
            "viewport_height": viewport_height,
            "page_height": page_height,
            "actual_delta": scroll_y - int(previous_scroll_y or 0),
            "is_scrollable": bool(page_height > viewport_height + 4),
            "at_bottom": at_bottom,
            "at_top": at_top,
            "can_scroll_more": bool(
                scroll_y < max(max_scroll - 4, 0) if direction == "down" else scroll_y > 4
            ),
        }

    def _browser_artifact_dir(self, context: Dict[str, Any] | None = None) -> Path:
        base_dir = Path(str((context or {}).get("output_dir") or tempfile.gettempdir()) or tempfile.gettempdir())
        artifact_dir = base_dir / "_browser_records"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir

    def _browser_evidence_enabled(self, context: Dict[str, Any] | None = None) -> bool:
        ctx = context if isinstance(context, dict) else self._active_plugin_context
        if not isinstance(ctx, dict):
            return False
        if bool(ctx.get("browser_save_evidence")):
            return True
        node_type = str(ctx.get("node_type") or "").strip().lower()
        return node_type in {"reviewer", "tester", "polisher"}

    def _browser_trace_enabled(self, context: Dict[str, Any] | None = None) -> bool:
        ctx = context if isinstance(context, dict) else self._active_plugin_context
        if isinstance(ctx, dict) and "browser_capture_trace" in ctx:
            return bool(ctx.get("browser_capture_trace"))
        raw = os.getenv("EVERMIND_BROWSER_CAPTURE_TRACE", "0")
        return str(raw or "").strip().lower() in ("1", "true", "yes", "on")

    def _browser_artifact_prefix(self, action: str, context: Dict[str, Any] | None = None) -> str:
        ctx = context if isinstance(context, dict) else self._active_plugin_context
        node_execution_id = str((ctx or {}).get("node_execution_id") or "").strip()
        run_id = str((ctx or {}).get("run_id") or "").strip()
        node_type = str((ctx or {}).get("node_type") or "browser").strip().lower() or "browser"
        safe_action = re.sub(r"[^a-z0-9_-]+", "_", str(action or "browser").strip().lower()) or "browser"
        base = node_execution_id or run_id or node_type
        safe_base = re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "browser"
        return f"{safe_base}_{node_type}_{safe_action}_{int(time.time() * 1000)}"

    async def _start_trace_for_action(self, action: str, context: Dict[str, Any] | None = None) -> bool:
        if not self._browser_evidence_enabled(context) or not self._browser_trace_enabled(context) or not self._context:
            return False
        if self._trace_active:
            try:
                await self._context.tracing.stop()
            except Exception:
                pass
            self._trace_active = False
        try:
            await self._context.tracing.start(screenshots=True, snapshots=True, sources=False)
            self._trace_active = True
            return True
        except Exception as exc:
            logger.warning("BrowserPlugin failed to start Playwright trace for %s: %s", action, exc)
            self._trace_active = False
            return False

    async def _stop_trace_for_action(self, action: str, context: Dict[str, Any] | None = None) -> Optional[Path]:
        if not self._trace_active or not self._context:
            return None
        artifact_dir = self._browser_artifact_dir(context)
        trace_path = artifact_dir / f"{self._browser_artifact_prefix(action, context)}.zip"
        try:
            await self._context.tracing.stop(path=str(trace_path))
            self._trace_active = False
            return trace_path if trace_path.exists() else None
        except Exception as exc:
            logger.warning("BrowserPlugin failed to stop Playwright trace for %s: %s", action, exc)
            self._trace_active = False
            return None

    def _write_browser_capture(
        self,
        screenshot_bytes: bytes,
        *,
        action: str,
        context: Dict[str, Any] | None = None,
    ) -> Optional[Path]:
        if not screenshot_bytes or not self._browser_evidence_enabled(context):
            return None
        artifact_dir = self._browser_artifact_dir(context)
        capture_path = artifact_dir / f"{self._browser_artifact_prefix(action, context)}.png"
        try:
            capture_path.write_bytes(screenshot_bytes)
            return capture_path
        except Exception as exc:
            logger.warning("BrowserPlugin failed to write browser capture %s: %s", capture_path, exc)
            return None

    async def _ensure_cursor_overlay(self, page) -> bool:
        """Inject a floating AI-cursor overlay so the user can watch the AI work.

        v5.8.5: matches Claude/Anthropic-style "AI driving your browser" UI.
        Paints a glowing dot that follows every mouse_click / mouse_move / drag
        plus a ripple effect on click. Idempotent — first call injects, later
        calls no-op. Gracefully degrades when the page is about:blank.
        """
        try:
            await page.evaluate(
                """
() => {
  if (window.__evermindCursorInstalled) return true;
  if (!document.documentElement) return false;
  window.__evermindCursorInstalled = true;
  const style = document.createElement('style');
  style.setAttribute('data-evermind-cursor-style', '1');
  style.textContent = `
    .__evermind_cursor_dot {
      position: fixed !important;
      left: 0; top: 0;
      width: 22px; height: 22px;
      border-radius: 50%;
      pointer-events: none !important;
      z-index: 2147483647 !important;
      background: radial-gradient(circle, rgba(91,140,255,0.95) 0%, rgba(91,140,255,0.45) 55%, rgba(91,140,255,0) 72%);
      border: 2px solid rgba(255,255,255,0.85);
      box-shadow: 0 0 18px rgba(91,140,255,0.75), 0 0 42px rgba(91,140,255,0.35), inset 0 0 6px rgba(255,255,255,0.6);
      transition: transform 140ms cubic-bezier(0.22, 1, 0.36, 1), opacity 200ms ease;
      transform: translate(-50%, -50%) scale(1);
      opacity: 0.95;
      mix-blend-mode: normal;
      will-change: transform;
    }
    .__evermind_cursor_label {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483647 !important;
      padding: 3px 8px;
      background: rgba(20, 24, 32, 0.85);
      color: #e8ecf3;
      border-radius: 4px;
      font: 600 11px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      letter-spacing: 0.02em;
      border: 1px solid rgba(91,140,255,0.55);
      backdrop-filter: blur(4px);
      transform: translate(14px, 14px);
      transition: left 140ms cubic-bezier(0.22, 1, 0.36, 1), top 140ms cubic-bezier(0.22, 1, 0.36, 1), opacity 200ms ease;
      opacity: 0.85;
    }
    .__evermind_cursor_ripple {
      position: fixed !important;
      left: 0; top: 0;
      width: 22px; height: 22px;
      border-radius: 50%;
      pointer-events: none !important;
      z-index: 2147483646 !important;
      border: 2px solid rgba(91,140,255,0.95);
      background: rgba(91,140,255,0.12);
      transform: translate(-50%, -50%) scale(0.5);
      animation: __evermind_cursor_ripple_anim 650ms cubic-bezier(0.22, 1, 0.36, 1) forwards;
    }
    @keyframes __evermind_cursor_ripple_anim {
      0%   { transform: translate(-50%, -50%) scale(0.4); opacity: 1; border-width: 3px; }
      60%  { opacity: 0.6; border-width: 2px; }
      100% { transform: translate(-50%, -50%) scale(5); opacity: 0; border-width: 1px; }
    }
    .__evermind_cursor_trail {
      position: fixed !important;
      left: 0; top: 0;
      width: 8px; height: 8px;
      border-radius: 50%;
      pointer-events: none !important;
      z-index: 2147483645 !important;
      background: rgba(91,140,255,0.5);
      transform: translate(-50%, -50%) scale(1);
      animation: __evermind_cursor_trail_anim 380ms cubic-bezier(0.4, 0, 0.2, 1) forwards;
    }
    @keyframes __evermind_cursor_trail_anim {
      0%   { opacity: 0.6; transform: translate(-50%, -50%) scale(1); }
      100% { opacity: 0; transform: translate(-50%, -50%) scale(0.3); }
    }
    .__evermind_cursor_key_toast {
      position: fixed !important;
      left: 50% !important;
      bottom: 32px !important;
      transform: translateX(-50%);
      pointer-events: none !important;
      z-index: 2147483647 !important;
      padding: 10px 22px;
      background: rgba(20, 24, 32, 0.92);
      color: #fff;
      border-radius: 10px;
      font: 600 14px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      border: 1px solid rgba(91,140,255,0.7);
      box-shadow: 0 12px 32px rgba(0,0,0,0.38);
      opacity: 0;
      animation: __evermind_cursor_key_toast_anim 820ms ease-out forwards;
    }
    @keyframes __evermind_cursor_key_toast_anim {
      0%   { opacity: 0; transform: translateX(-50%) translateY(12px); }
      20%  { opacity: 1; transform: translateX(-50%) translateY(0); }
      70%  { opacity: 1; transform: translateX(-50%) translateY(0); }
      100% { opacity: 0; transform: translateX(-50%) translateY(-8px); }
    }
    /* v6.0: scroll edge glow */
    .__evermind_scroll_glow {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483646 !important;
      animation: __evermind_scroll_glow_anim 1200ms ease-out forwards;
      will-change: opacity;
    }
    .__evermind_scroll_glow.down  { bottom: 0; left: 0; right: 0; height: 140px;
      background: linear-gradient(to top, rgba(73,80,246,0.55), transparent); }
    .__evermind_scroll_glow.up    { top: 0;    left: 0; right: 0; height: 140px;
      background: linear-gradient(to bottom, rgba(73,80,246,0.55), transparent); }
    .__evermind_scroll_glow.left  { top: 0; bottom: 0; left: 0; width: 140px;
      background: linear-gradient(to right, rgba(73,80,246,0.55), transparent); }
    .__evermind_scroll_glow.right { top: 0; bottom: 0; right: 0; width: 140px;
      background: linear-gradient(to left, rgba(73,80,246,0.55), transparent); }
    @keyframes __evermind_scroll_glow_anim {
      0%   { opacity: 0.0; }
      15%  { opacity: 1.0; }
      70%  { opacity: 0.7; }
      100% { opacity: 0.0; }
    }
    /* v6.0: element highlight (browser-use style target box) */
    .__evermind_element_highlight {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483645 !important;
      box-sizing: border-box;
      border: 2px solid rgba(91,140,255,0.95);
      background: rgba(91,140,255,0.12);
      border-radius: 4px;
      animation: __evermind_element_pulse 1400ms ease-in-out infinite;
    }
    @keyframes __evermind_element_pulse {
      0%,100% { box-shadow: 0 0 0 0 rgba(91,140,255,0.55); }
      50%     { box-shadow: 0 0 0 8px rgba(91,140,255,0); }
    }
    .__evermind_element_label {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483647 !important;
      font: 700 11px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: rgba(91,140,255,0.95);
      color: #fff;
      padding: 1px 6px;
      border-radius: 4px;
      letter-spacing: 0.02em;
    }
    /* v6.0: typed char float-up (rrweb-inspired) */
    .__evermind_type_char {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483647 !important;
      color: #5b8cff;
      font-weight: 700;
      font-size: 16px;
      text-shadow: 0 1px 2px rgba(0,0,0,0.45);
      transform: translate(-50%, -50%);
      animation: __evermind_type_char_anim 560ms ease-out forwards;
    }
    @keyframes __evermind_type_char_anim {
      0%   { opacity: 1.0; transform: translate(-50%, -50%); }
      80%  { opacity: 0.8; transform: translate(-50%, -140%); }
      100% { opacity: 0.0; transform: translate(-50%, -180%); }
    }
    /* v6.0: double-click / drag distinguishers */
    .__evermind_cursor_ripple.dblclick {
      animation-duration: 480ms;
      border-color: rgba(255,182,67,0.95);
    }
    .__evermind_cursor_ripple.drag {
      animation: __evermind_cursor_drag_anim 900ms ease-out forwards;
      border-color: rgba(205,96,217,0.95);
    }
    @keyframes __evermind_cursor_drag_anim {
      0%   { transform: translate(-50%, -50%) scale(0.5); opacity: 1; }
      100% { transform: translate(-50%, -50%) scale(3); opacity: 0; border-width: 3px; }
    }

    /* v6.1.14c (maintainer 2026-04-20): previous version used 60-90px inset
       box-shadow which bled blue/purple through the entire viewport. maintainer
       asked for ONLY a light purple gradient on the edge frame — not a
       full-screen tint. Stripped all inset shadows; kept a narrow outer
       glow so the border still reads as "lit up" without discoloring page
       content. Gradient tuned to soft lavender → pale cyan for subtlety. */
    /* v6.1.15 (maintainer 2026-04-20): previous border-box conic-gradient trick
       bled into padding-box on retina displays, washing the ENTIRE page
       in purple. Root cause: Chrome renders transparent padding-box layer
       on top of page content with ≥1px anti-alias halo on retina.
       Fix: plain solid purple border + pure outer glow. No background
       tricks. Guaranteed NO page tint. */
    .__evermind_ai_frame {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483640 !important;
      top: 0; left: 0; right: 0; bottom: 0;
      border: 3px solid rgba(180, 170, 255, 0.85);
      border-radius: 10px;
      box-sizing: border-box;
      background: transparent !important;
      box-shadow: 0 0 14px rgba(180, 170, 255, 0.5);
      opacity: 0;
      transition: opacity 280ms ease;
      animation: __evermind_ai_frame_pulse 2400ms ease-in-out infinite;
    }
    .__evermind_ai_frame.active { opacity: 1; }
    @keyframes __evermind_ai_frame_pulse {
      0%, 100% {
        border-color: rgba(180, 170, 255, 0.8);
        box-shadow: 0 0 12px rgba(180, 170, 255, 0.45);
      }
      50% {
        border-color: rgba(200, 180, 255, 0.95);
        box-shadow: 0 0 22px rgba(200, 180, 255, 0.65);
      }
    }

    /* v6.1.15: keyboard key HUD (maintainer asked — user should see W/A/S/D press in real-time) */
    .__evermind_key_hud {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483646 !important;
      left: 20px; bottom: 70px;
      display: flex; flex-direction: column; gap: 6px;
      font: 700 13px -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
    }
    .__evermind_key_hud_row { display: flex; gap: 6px; }
    .__evermind_key_chip {
      min-width: 32px; height: 32px;
      padding: 0 10px;
      display: inline-flex; align-items: center; justify-content: center;
      background: rgba(20, 24, 32, 0.85);
      color: #e8ecf3;
      border: 1px solid rgba(180, 170, 255, 0.6);
      border-radius: 6px;
      box-shadow: 0 2px 6px rgba(0,0,0,0.35);
      backdrop-filter: blur(4px);
      transition: transform 100ms ease-out, background 150ms ease-out, border-color 150ms ease-out, box-shadow 150ms ease-out;
    }
    .__evermind_key_chip.pressed {
      background: rgba(180, 170, 255, 0.95);
      color: #0c0c12;
      border-color: rgba(220, 210, 255, 1);
      box-shadow: 0 0 14px rgba(180, 170, 255, 0.95), 0 2px 6px rgba(0,0,0,0.35);
      transform: translateY(1px) scale(0.96);
    }
    .__evermind_ai_badge {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483646 !important;
      bottom: 18px;
      left: 50%;
      transform: translateX(-50%);
      padding: 6px 14px 6px 10px;
      background: rgba(14, 18, 32, 0.88);
      color: #c3a8ff;
      border: 1px solid rgba(155, 130, 255, 0.8);
      border-radius: 999px;
      font: 700 12px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      letter-spacing: 0.04em;
      backdrop-filter: blur(6px);
      box-shadow: 0 4px 18px rgba(0,0,0,0.4), 0 0 28px rgba(155, 130, 255, 0.55);
      opacity: 0;
      transition: opacity 240ms ease;
      display: flex; align-items: center; gap: 6px;
    }
    .__evermind_ai_badge.active { opacity: 0.95; }
    .__evermind_ai_badge::before {
      content: ''; display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      background: #b28bff;
      box-shadow: 0 0 12px #b28bff, 0 0 4px #b28bff;
      animation: __evermind_ai_badge_dot 1100ms ease-in-out infinite;
    }
    @keyframes __evermind_ai_badge_dot {
      0%, 100% { transform: scale(0.82); opacity: 0.75; }
      50%      { transform: scale(1.1);  opacity: 1.0; }
    }
  `;
  document.documentElement.appendChild(style);
  const dot = document.createElement('div');
  dot.className = '__evermind_cursor_dot';
  dot.style.left = '0px';
  dot.style.top = '0px';
  document.documentElement.appendChild(dot);
  const label = document.createElement('div');
  label.className = '__evermind_cursor_label';
  label.textContent = 'AI';
  label.style.left = '0px';
  label.style.top = '0px';
  label.style.opacity = '0';
  document.documentElement.appendChild(label);

  // v6.1.3: "AI is controlling" full-page frame + badge
  const aiFrame = document.createElement('div');
  aiFrame.className = '__evermind_ai_frame';
  document.documentElement.appendChild(aiFrame);
  const aiBadge = document.createElement('div');
  aiBadge.className = '__evermind_ai_badge';
  aiBadge.textContent = 'AI 正在控制浏览器';
  document.documentElement.appendChild(aiBadge);

  // v6.1.15: keyboard HUD — when AI presses W/A/S/D/Space/E/Shift etc.,
  // show the key visually in bottom-left so user can see what AI is doing.
  const keyHud = document.createElement('div');
  keyHud.className = '__evermind_key_hud';
  keyHud.innerHTML = `
    <div class="__evermind_key_hud_row" data-row="1">
      <div class="__evermind_key_chip" data-key="W">W</div>
    </div>
    <div class="__evermind_key_hud_row" data-row="2">
      <div class="__evermind_key_chip" data-key="A">A</div>
      <div class="__evermind_key_chip" data-key="S">S</div>
      <div class="__evermind_key_chip" data-key="D">D</div>
    </div>
    <div class="__evermind_key_hud_row" data-row="3">
      <div class="__evermind_key_chip" data-key=" ">Space</div>
      <div class="__evermind_key_chip" data-key="Shift">⇧</div>
      <div class="__evermind_key_chip" data-key="E">E</div>
    </div>
  `;
  keyHud.style.display = 'none';
  document.documentElement.appendChild(keyHud);
  const __showKeyHud = () => { keyHud.style.display = 'flex'; };
  const __flashKeyChip = (key) => {
    try {
      const norm = String(key || '').trim();
      const chips = keyHud.querySelectorAll('.__evermind_key_chip');
      chips.forEach(c => {
        const target = c.getAttribute('data-key');
        if (!target) return;
        // Match plain key (W/a/S/d), arrows, space, shift, etc.
        const matchesLower = target.toLowerCase() === norm.toLowerCase();
        const matchesArrow = (target === 'ArrowUp' && norm === 'ArrowUp')
                          || (target === 'ArrowDown' && norm === 'ArrowDown');
        if (matchesLower || matchesArrow) {
          c.classList.add('pressed');
          setTimeout(() => { try { c.classList.remove('pressed'); } catch(_){} }, 280);
        }
      });
    } catch(_) {}
  };
  // Also listen to real keyboard events so pressed chips reflect any key
  // that the AI dispatches via Playwright keyboard.press() which becomes a
  // real DOM KeyboardEvent.
  document.addEventListener('keydown', (e) => {
    if (!aiFrame.classList.contains('active')) return;
    __showKeyHud();
    __flashKeyChip(e.key);
  }, true);
  /* v6.1.3 (maintainer 2026-04-18): frame stays ACTIVE for the whole AI session.
     Previous 3.2s auto-hide meant the "AI is controlling" signal flickered
     between actions, giving the impression nothing was happening during
     pauses (e.g. CoVe reasoning, captcha wait). Session ends only via
     explicit setAiActive(false) from shutdown. Badge text updates per-action
     for context, but frame visibility is binary per-session. */
  let __aiFrameIdleTimer = null;
  const __showAiFrame = (lbl) => {
    aiFrame.classList.add('active');
    aiBadge.classList.add('active');
    if (typeof lbl === 'string' && lbl) {
      aiBadge.textContent = 'AI · ' + lbl;
    } else {
      aiBadge.textContent = 'AI 正在控制浏览器';
    }
    if (__aiFrameIdleTimer) clearTimeout(__aiFrameIdleTimer);
    // 10-minute idle fallback: if no action fires for 600s, fade out in case
    // an orphan overlay leaked from a crashed session.
    __aiFrameIdleTimer = setTimeout(() => {
      aiFrame.classList.remove('active');
      aiBadge.classList.remove('active');
    }, 600000);
  };
  const __hideAiFrame = () => {
    aiFrame.classList.remove('active');
    aiBadge.classList.remove('active');
    if (__aiFrameIdleTimer) { clearTimeout(__aiFrameIdleTimer); __aiFrameIdleTimer = null; }
  };
  /* Keep cursor dot minimally visible while AI active (not just on click). */
  const __centerDotIfUnset = () => {
    if (!dot.__evermind_positioned) {
      dot.style.left = (window.innerWidth * 0.5) + 'px';
      dot.style.top = (window.innerHeight * 0.5) + 'px';
      dot.__evermind_positioned = true;
    }
  };
  window.__evermindCursor = {
    aiControlling: __showAiFrame,
    hideAi: __hideAiFrame,
    centerDot: __centerDotIfUnset,
    move: (x, y, lbl) => {
      __showAiFrame(lbl || 'moving');
      dot.__evermind_positioned = true;
      dot.style.left = x + 'px';
      dot.style.top = y + 'px';
      label.style.left = x + 'px';
      label.style.top = y + 'px';
      if (typeof lbl === 'string' && lbl) label.textContent = lbl;
      label.style.opacity = '0.9';
      const trail = document.createElement('div');
      trail.className = '__evermind_cursor_trail';
      trail.style.left = x + 'px';
      trail.style.top = y + 'px';
      document.documentElement.appendChild(trail);
      setTimeout(() => { try { trail.remove(); } catch(_){} }, 420);
    },
    click: (x, y, button) => {
      __showAiFrame(button === 'right' ? '右键' : '点击');
      dot.style.left = x + 'px';
      dot.style.top = y + 'px';
      label.style.left = x + 'px';
      label.style.top = y + 'px';
      label.style.opacity = '0.95';
      const ripple = document.createElement('div');
      ripple.className = '__evermind_cursor_ripple';
      ripple.style.left = x + 'px';
      ripple.style.top = y + 'px';
      if (button === 'right') ripple.style.borderColor = 'rgba(255,91,71,0.95)';
      else if (button === 'middle') ripple.style.borderColor = 'rgba(72,207,173,0.95)';
      document.documentElement.appendChild(ripple);
      dot.style.transform = 'translate(-50%, -50%) scale(0.72)';
      setTimeout(() => { dot.style.transform = 'translate(-50%, -50%) scale(1)'; }, 180);
      setTimeout(() => { try { ripple.remove(); } catch(_){} }, 720);
    },
    key: (keyLabel) => {
      __showAiFrame('按键 ' + String(keyLabel || '').slice(0, 12));
      __showKeyHud();
      __flashKeyChip(keyLabel);
      const toast = document.createElement('div');
      toast.className = '__evermind_cursor_key_toast';
      toast.textContent = 'Key · ' + String(keyLabel || '').slice(0, 32);
      document.documentElement.appendChild(toast);
      setTimeout(() => { try { toast.remove(); } catch(_){} }, 900);
    },
    /* v6.0: scroll edge glow. direction ∈ up/down/left/right */
    scroll: (direction) => {
      const dirName = String(direction || 'down').toLowerCase();
      __showAiFrame('滚动 ' + dirName);
      const glow = document.createElement('div');
      glow.className = '__evermind_scroll_glow ' + dirName;
      document.documentElement.appendChild(glow);
      setTimeout(() => { try { glow.remove(); } catch(_){} }, 1240);
    },
    /* v6.0: highlight a rect on the page (browser-use target box).
       rect = {top,left,width,height}, idx = optional [N] label */
    highlight: (rect, idx, ttlMs) => {
      if (!rect || typeof rect !== 'object') return;
      const box = document.createElement('div');
      box.className = '__evermind_element_highlight';
      box.style.top = rect.top + 'px';
      box.style.left = rect.left + 'px';
      box.style.width = rect.width + 'px';
      box.style.height = rect.height + 'px';
      document.documentElement.appendChild(box);
      let lbl = null;
      if (typeof idx === 'number' || (typeof idx === 'string' && idx.length)) {
        lbl = document.createElement('div');
        lbl.className = '__evermind_element_label';
        lbl.textContent = '[' + idx + ']';
        lbl.style.top = (rect.top - 16) + 'px';
        lbl.style.left = rect.left + 'px';
        document.documentElement.appendChild(lbl);
      }
      const lifetime = typeof ttlMs === 'number' ? ttlMs : 2400;
      setTimeout(() => {
        try { box.remove(); } catch(_){}
        if (lbl) { try { lbl.remove(); } catch(_){} }
      }, lifetime);
    },
    /* v6.0: typed character floats up from the active caret */
    type: (x, y, ch) => {
      const node = document.createElement('div');
      node.className = '__evermind_type_char';
      node.textContent = String(ch || '').slice(0, 4);
      node.style.left = x + 'px';
      node.style.top = y + 'px';
      document.documentElement.appendChild(node);
      setTimeout(() => { try { node.remove(); } catch(_){} }, 620);
    },
    /* v6.0: double-click — a second ripple with warmer color */
    dblclick: (x, y) => {
      const ripple = document.createElement('div');
      ripple.className = '__evermind_cursor_ripple dblclick';
      ripple.style.left = x + 'px';
      ripple.style.top = y + 'px';
      document.documentElement.appendChild(ripple);
      setTimeout(() => { try { ripple.remove(); } catch(_){} }, 540);
    },
    /* v6.0: drag — an elongated ripple animation on start point */
    drag: (fromX, fromY, toX, toY) => {
      const ripple = document.createElement('div');
      ripple.className = '__evermind_cursor_ripple drag';
      ripple.style.left = fromX + 'px';
      ripple.style.top = fromY + 'px';
      document.documentElement.appendChild(ripple);
      setTimeout(() => { try { ripple.remove(); } catch(_){} }, 960);
      /* v6.1.15 (maintainer 2026-04-20): also draw an SVG path line from→to
         so user can see drag trajectory (critical for 3D camera drag
         review in games). */
      try {
        if (typeof toX === 'number' && typeof toY === 'number') {
          const svgNs = 'http://www.w3.org/2000/svg';
          const svg = document.createElementNS(svgNs, 'svg');
          svg.setAttribute('class', '__evermind_drag_path');
          svg.style.position = 'fixed';
          svg.style.left = '0';
          svg.style.top = '0';
          svg.style.width = '100%';
          svg.style.height = '100%';
          svg.style.zIndex = '2147483644';
          svg.style.pointerEvents = 'none';
          const line = document.createElementNS(svgNs, 'line');
          line.setAttribute('x1', fromX);
          line.setAttribute('y1', fromY);
          line.setAttribute('x2', toX);
          line.setAttribute('y2', toY);
          line.setAttribute('stroke', 'rgba(180, 170, 255, 0.85)');
          line.setAttribute('stroke-width', '2.5');
          line.setAttribute('stroke-linecap', 'round');
          line.setAttribute('stroke-dasharray', '6 6');
          svg.appendChild(line);
          const arrow = document.createElementNS(svgNs, 'circle');
          arrow.setAttribute('cx', toX);
          arrow.setAttribute('cy', toY);
          arrow.setAttribute('r', '4');
          arrow.setAttribute('fill', 'rgba(205, 96, 217, 0.95)');
          svg.appendChild(arrow);
          document.documentElement.appendChild(svg);
          setTimeout(() => { try { svg.remove(); } catch(_){} }, 1200);
        }
      } catch(_) {}
    },
    /* v6.1.15 (maintainer 2026-04-20): hold/long-press — circular progress ring
       around the cursor that fills during hold duration. Used for games
       where AI long-presses to charge attack / fire continuous bullets. */
    hold: (x, y, durationMs, label) => {
      __showAiFrame(label || '长按');
      const svgNs = 'http://www.w3.org/2000/svg';
      const ring = document.createElementNS(svgNs, 'svg');
      ring.setAttribute('class', '__evermind_hold_ring');
      ring.style.position = 'fixed';
      ring.style.left = (x - 24) + 'px';
      ring.style.top = (y - 24) + 'px';
      ring.style.width = '48px';
      ring.style.height = '48px';
      ring.style.zIndex = '2147483645';
      ring.style.pointerEvents = 'none';
      ring.innerHTML = `
        <circle cx="24" cy="24" r="20" fill="none"
                stroke="rgba(180,170,255,0.28)" stroke-width="3"/>
        <circle cx="24" cy="24" r="20" fill="none"
                stroke="rgba(205,96,217,0.95)" stroke-width="3"
                stroke-linecap="round"
                stroke-dasharray="125.66" stroke-dashoffset="125.66"
                transform="rotate(-90 24 24)">
          <animate attributeName="stroke-dashoffset"
                   from="125.66" to="0"
                   dur="${Math.max(200, durationMs || 800)}ms"
                   fill="freeze"/>
        </circle>`;
      document.documentElement.appendChild(ring);
      setTimeout(() => { try { ring.remove(); } catch(_){} }, Math.max(300, (durationMs || 800) + 200));
    },
    hide: () => { dot.style.opacity = '0'; label.style.opacity = '0'; },
    show: () => { dot.style.opacity = '0.95'; label.style.opacity = '0.9'; },
  };
  /* v6.1.3 (maintainer 2026-04-18): immediately surface the AI frame after
     install so the user sees the cyan-purple glow even on pages where AI
     only observes (no click/move). centerDot keeps the cursor at viewport
     centre so it's visible during hover-only / observe-only sessions. */
  try { __centerDotIfUnset(); } catch(_) {}
  try { __showAiFrame('已接管浏览器'); } catch(_) {}
  return true;
}
                """
            )
            return True
        except Exception as exc:
            logger.debug("cursor overlay inject failed: %s", exc)
            return False

    async def _visualize_ai_session_start(self, page, label: str = "已接管浏览器") -> None:
        """Explicitly turn on the AI-controlling frame at session start."""
        try:
            await page.evaluate(
                "(lbl) => { try { if (window.__evermindCursor) window.__evermindCursor.aiControlling(lbl); } catch(e){} }",
                str(label or ""),
            )
        except Exception:
            pass

    async def _visualize_ai_session_end(self, page) -> None:
        """Turn off AI frame when the session ends (shutdown / navigate-away)."""
        try:
            await page.evaluate(
                "() => { try { if (window.__evermindCursor && window.__evermindCursor.hideAi) window.__evermindCursor.hideAi(); } catch(e){} }"
            )
        except Exception:
            pass

    async def _visualize_cursor_move(self, page, x: float, y: float, label: str = "AI") -> None:
        """Slide the AI cursor to (x,y) before the real playwright move lands."""
        try:
            await page.evaluate(
                "([x, y, lbl]) => { try { if (window.__evermindCursor) window.__evermindCursor.move(x, y, lbl); } catch(e){} }",
                [float(x), float(y), label],
            )
        except Exception:
            pass

    async def _visualize_cursor_click(self, page, x: float, y: float, button: str = "left") -> None:
        """Ripple + cursor punch at (x,y)."""
        try:
            await page.evaluate(
                "([x, y, btn]) => { try { if (window.__evermindCursor) window.__evermindCursor.click(x, y, btn); } catch(e){} }",
                [float(x), float(y), button],
            )
        except Exception:
            pass

    async def _visualize_cursor_key(self, page, key_label: str) -> None:
        """Toast 'Key · X' at the bottom so the user can see keyboard input."""
        try:
            await page.evaluate(
                "(k) => { try { if (window.__evermindCursor) window.__evermindCursor.key(k); } catch(e){} }",
                str(key_label or ""),
            )
        except Exception:
            pass

    # v6.0: scroll / highlight / type / dblclick / drag visualizations.
    async def _visualize_scroll(self, page, direction: str = "down") -> None:
        try:
            await page.evaluate(
                "(d) => { try { if (window.__evermindCursor) window.__evermindCursor.scroll(d); } catch(e){} }",
                str(direction or "down"),
            )
        except Exception:
            pass

    async def _visualize_highlight(
        self,
        page,
        rect: Dict[str, float],
        idx: Optional[Any] = None,
        ttl_ms: int = 2400,
    ) -> None:
        try:
            await page.evaluate(
                "([r, i, t]) => { try { if (window.__evermindCursor) window.__evermindCursor.highlight(r, i, t); } catch(e){} }",
                [rect, idx, int(ttl_ms)],
            )
        except Exception:
            pass

    async def _visualize_type_char(self, page, x: float, y: float, ch: str) -> None:
        try:
            await page.evaluate(
                "([x, y, c]) => { try { if (window.__evermindCursor) window.__evermindCursor.type(x, y, c); } catch(e){} }",
                [float(x), float(y), str(ch or "")[:4]],
            )
        except Exception:
            pass

    async def _visualize_dblclick(self, page, x: float, y: float) -> None:
        try:
            await page.evaluate(
                "([x, y]) => { try { if (window.__evermindCursor) window.__evermindCursor.dblclick(x, y); } catch(e){} }",
                [float(x), float(y)],
            )
        except Exception:
            pass

    async def _visualize_drag(self, page, fx: float, fy: float, tx: float, ty: float) -> None:
        try:
            await page.evaluate(
                "([a, b, c, d]) => { try { if (window.__evermindCursor) window.__evermindCursor.drag(a, b, c, d); } catch(e){} }",
                [float(fx), float(fy), float(tx), float(ty)],
            )
        except Exception:
            pass

    def _cursor_overlay_enabled(self, context: Dict[str, Any] | None = None) -> bool:
        """Opt out only when explicitly disabled — default ON for visibility."""
        ctx = context if isinstance(context, dict) else self._active_plugin_context
        if isinstance(ctx, dict) and "browser_show_ai_cursor" in ctx:
            return bool(ctx.get("browser_show_ai_cursor"))
        raw = os.getenv("EVERMIND_BROWSER_SHOW_AI_CURSOR", "1")
        return str(raw or "").strip().lower() in ("1", "true", "yes", "on")

    async def _bbox_center(self, locator) -> Tuple[Optional[float], Optional[float]]:
        """Resolve bbox center of a locator (for visual feedback on selector-based clicks)."""
        try:
            box = await locator.bounding_box()
            if isinstance(box, dict) and box:
                return (
                    float(box.get("x", 0) or 0) + float(box.get("width", 0) or 0) / 2.0,
                    float(box.get("y", 0) or 0) + float(box.get("height", 0) or 0) / 2.0,
                )
        except Exception:
            pass
        return (None, None)

    def _annotate_screenshot(
        self,
        screenshot_bytes: bytes,
        snapshot: Optional[Dict[str, Any]],
        *,
        max_boxes: int = 40,
    ) -> bytes:
        """Draw ref-N boxes on the screenshot so a VLM can ground clicks visually.

        v5.8.4: matches browser-use's vision grounding — bbox + index label painted
        directly on the screenshot, paired with the same index in the text state.
        """
        if PILImage is None or ImageDraw is None or not screenshot_bytes:
            return screenshot_bytes
        if not isinstance(snapshot, dict):
            return screenshot_bytes
        interactive = snapshot.get("interactive")
        if not isinstance(interactive, list) or not interactive:
            return screenshot_bytes
        try:
            img = PILImage.open(io.BytesIO(screenshot_bytes)).convert("RGBA")
            overlay = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            viewport = snapshot.get("viewport") if isinstance(snapshot.get("viewport"), dict) else {}
            vw = int(viewport.get("w") or img.width)
            vh = int(viewport.get("h") or img.height)
            scale_x = img.width / max(1, vw)
            scale_y = img.height / max(1, vh)
            palette = [
                (255, 91, 71),   # tomato
                (91, 140, 255),  # blue
                (72, 207, 173),  # teal
                (255, 182, 67),  # amber
                (205, 96, 217),  # purple
                (120, 215, 78),  # lime
            ]
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
            painted = 0
            for item in interactive:
                if painted >= max_boxes:
                    break
                if not isinstance(item, dict):
                    continue
                bbox = item.get("bbox") if isinstance(item.get("bbox"), dict) else None
                if not bbox:
                    continue
                x = int(round(float(bbox.get("x", 0) or 0) * scale_x))
                y = int(round(float(bbox.get("y", 0) or 0) * scale_y))
                w = int(round(float(bbox.get("w", 0) or 0) * scale_x))
                h = int(round(float(bbox.get("h", 0) or 0) * scale_y))
                if w < 4 or h < 4:
                    continue
                color = palette[painted % len(palette)]
                outline = color + (255,)
                fill = color + (48,)
                draw.rectangle([x, y, x + w, y + h], outline=outline, fill=fill, width=2)
                idx_label = str(item.get("index") or item.get("ref") or painted + 1)
                label_text = f" {idx_label} "
                if font is not None:
                    try:
                        tb = draw.textbbox((0, 0), label_text, font=font)
                        tw, th = tb[2] - tb[0], tb[3] - tb[1]
                    except Exception:
                        tw, th = 12, 10
                else:
                    tw, th = 12, 10
                label_x = max(0, min(x, img.width - tw - 4))
                label_y = max(0, min(y - th - 2, img.height - th - 2))
                draw.rectangle(
                    [label_x, label_y, label_x + tw + 4, label_y + th + 2],
                    fill=color + (220,),
                )
                if font is not None:
                    draw.text((label_x + 2, label_y + 1), label_text, fill=(255, 255, 255, 255), font=font)
                else:
                    draw.text((label_x + 2, label_y + 1), label_text, fill=(255, 255, 255, 255))
                painted += 1
            out = PILImage.alpha_composite(img, overlay).convert("RGB")
            buf = io.BytesIO()
            out.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
        except Exception as exc:
            logger.warning("BrowserPlugin failed to annotate screenshot: %s", exc)
            return screenshot_bytes

    def _write_scroll_gif(
        self,
        frame_bytes: List[bytes],
        *,
        output_path: Path,
        duration_ms: int,
    ) -> bool:
        if PILImage is None or not frame_bytes:
            return False
        try:
            frames = [PILImage.open(io.BytesIO(blob)).convert("RGBA") for blob in frame_bytes]
            first, rest = frames[0], frames[1:]
            first.save(
                output_path,
                save_all=True,
                append_images=rest,
                duration=max(40, min(int(duration_ms or 220), 5000)),
                loop=0,
                disposal=2,
            )
            return output_path.exists()
        except Exception as exc:
            logger.warning("BrowserPlugin failed to write scroll GIF %s: %s", output_path, exc)
            return False

    async def _finalize_browser_result(
        self,
        page,
        *,
        action: str,
        base_data: Optional[Dict[str, Any]] = None,
        include_screenshot: bool = True,
        full_page: bool = False,
        include_snapshot: bool = False,
        snapshot_limit: int = 30,
        snapshot_override: Optional[Dict[str, Any]] = None,
    ) -> PluginResult:
        data: Dict[str, Any] = {
            "action": action,
            "url": page.url,
            **(base_data or {}),
            "browser_mode": "headless" if self._headless else "headful",
            "requested_mode": "headless" if self._requested_headless else "headful",
            "launch_note": self._launch_note,
        }
        artifacts: List[Dict[str, Any]] = []
        screenshot_bytes = b""
        if include_screenshot:
            screenshot_bytes = await page.screenshot(full_page=full_page)
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            artifacts.append({"type": "image", "base64": b64})
            capture_path = self._write_browser_capture(screenshot_bytes, action=action)
            if capture_path is not None:
                artifacts.append({"type": "image", "path": str(capture_path)})
                data["capture_path"] = str(capture_path)
            previous_hash = self._last_state_hash or ""
            state_hash = hashlib.sha1(screenshot_bytes).hexdigest()[:16]
            data["state_hash"] = state_hash
            data["previous_state_hash"] = previous_hash
            data["state_changed"] = bool(previous_hash) and previous_hash != state_hash
            self._last_state_hash = state_hash
        if include_snapshot:
            snapshot = snapshot_override if isinstance(snapshot_override, dict) else await self._page_snapshot(page, limit=snapshot_limit)
            data["snapshot"] = snapshot
            ref_preview = self._snapshot_ref_preview(snapshot, limit=8)
            if ref_preview:
                data["snapshot_refs_preview"] = ref_preview
                data["snapshot_ref_count"] = len(snapshot.get("interactive", [])) if isinstance(snapshot, dict) else len(ref_preview)
            # v5.8.4: vision grounding — emit an annotated screenshot alongside
            # the text snapshot so a VLM can see which [index] lands where.
            # Activated when we already took a screenshot this turn.
            if screenshot_bytes and isinstance(snapshot, dict) and snapshot.get("interactive"):
                annotated = self._annotate_screenshot(screenshot_bytes, snapshot)
                if annotated and annotated is not screenshot_bytes:
                    annotated_b64 = base64.b64encode(annotated).decode("utf-8")
                    artifacts.append({"type": "image", "base64": annotated_b64, "variant": "annotated"})
                    annotated_path = self._write_browser_capture(annotated, action=f"{action}_annotated")
                    if annotated_path is not None:
                        artifacts.append({"type": "image", "path": str(annotated_path), "variant": "annotated"})
                        data["annotated_capture_path"] = str(annotated_path)
        diagnostics = self._diagnostics_summary()
        page_metrics = await self._get_scroll_metrics(page)
        if isinstance(page_metrics, dict):
            scroll_meta = self._scroll_metadata(page_metrics, direction="down")
            data.update({
                "scroll_y": int(scroll_meta.get("scroll_y", 0) or 0),
                "viewport_height": int(scroll_meta.get("viewport_height", 0) or 0),
                "page_height": int(scroll_meta.get("page_height", 0) or 0),
                "at_page_top": bool(scroll_meta.get("at_top")),
                "at_page_bottom": bool(scroll_meta.get("at_bottom")),
            })
        data.update(diagnostics)
        trace_path = await self._stop_trace_for_action(action)
        if trace_path is not None:
            data["trace_path"] = str(trace_path)
            artifacts.append({"type": "trace", "path": str(trace_path)})
        self._action_log.append({
            "action": action,
            "url": page.url,
            "state_hash": data.get("state_hash", ""),
        })
        self._action_log = self._action_log[-20:]
        # v6.4.27 (maintainer 2026-04-22): restore focus ONLY after page-changing
        # actions. Previously this fired after EVERY action (observe /
        # snapshot / scroll / click / navigate) — 2 osascript calls per
        # action adds ~50-100ms and the focus flicker was still noticeable
        # over 15-round reviewer runs. With Fix 1 (bring_to_front removed)
        # Chromium no longer activates itself during observe/snapshot/scroll,
        # so only `navigate` can still pull focus when the tab loads a URL.
        # Restrict restore to page-changing actions: `navigate`, `goto`,
        # `reload`. Skip for `observe` / `snapshot` / `scroll` / `click` /
        # `extract` / `keyboard` etc. where v6.4.27 shouldn't leak focus.
        _page_changing_actions = {"navigate", "goto", "reload", "back", "forward"}
        if str(action or "").strip().lower() in _page_changing_actions:
            self._restore_user_focus_nonblocking()
        return PluginResult(success=True, data=data, artifacts=artifacts)

    def _restore_user_focus_nonblocking(self) -> None:
        """Non-blocking focus restoration after a headful browser action.

        v6.4.14 (maintainer 2026-04-22) REDESIGN. The v6.4.11 version hard-coded
        "activate Evermind", which just moved the focus-steal problem from
        Chromium to Evermind — if the user was working in Antigravity / a
        terminal / another app, we yanked them out of it every browser call.

        Correct behaviour: restore the frontmost app that was active *when
        the browser session was opened* (captured in `_ensure_browser`).
        Works for any user workflow:
            Evermind open + user looking at Evermind   → restore Evermind
            Evermind open + user editing in Antigravity → restore Antigravity
            Evermind open + user replying in Terminal  → restore Terminal

        If we somehow never captured a "before" app, fall back to the env
        override `EVERMIND_BROWSER_FOCUS_TARGET` (default Evermind). Skipped
        entirely when headless or the user opts out via EVERMIND_BROWSER_KEEP_FOCUS=0.
        """
        if getattr(self, "_headless", False):
            return
        if str(os.getenv("EVERMIND_BROWSER_KEEP_FOCUS", "1")).strip().lower() in {"0", "false", "no", "off"}:
            return
        captured_app = str(getattr(self, "_pre_launch_frontmost_app", "") or "").strip()
        # Never re-activate Chromium/Chrome/Evermind-Helper — those are the
        # browser windows we're trying to push back into the background.
        if captured_app and not re.search(r"(?:chrome|chromium|evermind\s+helper)", captured_app, re.IGNORECASE):
            target_app = captured_app
        else:
            target_app = str(os.getenv("EVERMIND_BROWSER_FOCUS_TARGET", "Evermind")).strip() or "Evermind"
        try:
            subprocess.Popen(
                [
                    "/usr/bin/osascript", "-e",
                    f'tell application "{target_app}" to activate',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _capture_pre_launch_frontmost_app(self) -> None:
        """Record the frontmost macOS app right before we launch the browser.
        `_restore_user_focus_nonblocking` uses this to send focus back to
        wherever the user actually was (may be Evermind, Antigravity,
        Terminal, iTerm, Finder, etc.). Cheap ~20-30ms AppleScript query.
        Best-effort; silent if anything fails."""
        try:
            result = subprocess.run(
                [
                    "/usr/bin/osascript", "-e",
                    'tell application "System Events" to get name of first application process whose frontmost is true',
                ],
                capture_output=True,
                text=True,
                timeout=0.8,
            )
            name = (result.stdout or "").strip()
            if name:
                self._pre_launch_frontmost_app = name
        except Exception:
            # Leave the attribute unset; restore will fall back to the env default.
            pass

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        action = str(params.get("action", "navigate") or "navigate").strip().lower()

        # v6.2 (maintainer 2026-04-20): record_preview creates a dedicated Playwright
        # context with record_video_dir, runs task-type-specific interactions,
        # and returns the resulting webm path. Kept OFF the shared browser
        # state so the main browsing session stays untouched.
        if action == "record_preview":
            return await self._record_preview_session(params, context or {})

        trace_started = False
        try:
            page = await self._ensure_browser(context=context)
            trace_started = await self._start_trace_for_action(action, context)
            url = str(params.get("url", "") or "").strip()
            if url and url.strip():
                if action == "navigate":
                    self._clear_diagnostics()
                # v7.11 (maintainer 2026-04-28): cache-bust /preview/ navigations.
                # User reported reviewer testing OLD artifact while latest
                # patcher edits sat on disk. Browser disk cache + Service
                # Worker can serve a stale HTML even when the server sends
                # Cache-Control: no-store. Forcing a unique query string per
                # navigation guarantees Playwright fetches the freshest bytes.
                _nav_url = url.strip()
                try:
                    if "/preview" in _nav_url:
                        import time as _time_v711
                        _sep = "&" if "?" in _nav_url else "?"
                        _nav_url = f"{_nav_url}{_sep}_evermind_ts={int(_time_v711.time()*1000)}"
                except Exception:
                    pass
                # Clear any in-memory cache for this navigation cycle so the
                # very first GET hits the server (not service-worker cache).
                try:
                    if action == "navigate":
                        await page.context.clear_cookies()
                except Exception:
                    pass
                await page.goto(_nav_url, wait_until="domcontentloaded")
            elif params.get("url") is not None:  # url was provided but empty/whitespace
                url = None  # treat as no URL provided

            if action == "navigate":
                if not url:
                    return PluginResult(success=False, error="navigate action requires a url")
                content = await page.content()
                title = await page.title()
                return await self._finalize_browser_result(
                    page,
                    action="navigate",
                    base_data={"title": title, "content_length": len(content)},
                    include_screenshot=True,
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=bool(params.get("include_snapshot", True)),
                    snapshot_limit=max(1, min(int(params.get("limit", 30) or 30), 80)),
                )

            if action == "observe":
                snapshot_limit = max(1, min(int(params.get("limit", 30) or 30), 80))
                goal = str(params.get("goal") or params.get("target") or "").strip()
                snapshot = await self._page_snapshot(page, limit=snapshot_limit)
                observation = self._summarize_snapshot(snapshot, goal=goal)
                return await self._finalize_browser_result(
                    page,
                    action="observe",
                    base_data={
                        "goal": goal,
                        "target": goal or None,
                        "observation": observation,
                        "snapshot_limit": snapshot_limit,
                    },
                    include_screenshot=bool(params.get("screenshot", True)),
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=True,
                    snapshot_limit=snapshot_limit,
                    snapshot_override=snapshot,
                )

            if action == "snapshot":
                snapshot_limit = max(1, min(int(params.get("limit", 30) or 30), 80))
                return await self._finalize_browser_result(
                    page,
                    action="snapshot",
                    base_data={"snapshot_limit": snapshot_limit},
                    include_screenshot=bool(params.get("screenshot", True)),
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=True,
                    snapshot_limit=snapshot_limit,
                )

            if action == "act":
                subaction = self._infer_act_subaction(params)
                locator, target = await self._resolve_locator(page, params)
                matched_item = None
                if locator is None:
                    target_hint = str(params.get("target") or params.get("goal") or "").strip()
                    if target_hint:
                        locator, target, matched_item = await self._resolve_target_hint(page, target_hint, intent=subaction)
                matched_ref = str((matched_item or {}).get("ref", "") or "").strip() if isinstance(matched_item, dict) else ""
                base_data: Dict[str, Any] = {
                    "subaction": subaction,
                    "intent": subaction,
                    "target": target or str(params.get("target") or "").strip() or None,
                    "matched_ref": matched_ref or None,
                }

                if subaction == "click":
                    if locator is None:
                        return PluginResult(success=False, error="act(click) requires a resolvable ref/target/selector/text/role/label/placeholder")
                    if self._cursor_overlay_enabled(context):
                        await self._ensure_cursor_overlay(page)
                        cx, cy = await self._bbox_center(locator)
                        if cx is not None and cy is not None:
                            await self._visualize_cursor_move(page, cx, cy, target or "click")
                            await page.wait_for_timeout(120)
                            await self._visualize_cursor_click(page, cx, cy, "left")
                    await locator.click(timeout=int(params.get("timeout_ms", 5000) or 5000))
                    await page.wait_for_timeout(params.get("wait_ms", 800))
                    return await self._finalize_browser_result(
                        page,
                        action="act",
                        base_data=base_data,
                        include_screenshot=True,
                        full_page=bool(params.get("full_page", False)),
                        include_snapshot=bool(params.get("include_snapshot", True)),
                    )

                if subaction == "fill":
                    value = params.get("value", "")
                    if locator is None:
                        return PluginResult(success=False, error="act(fill) requires a resolvable input target")
                    await locator.fill(value)
                    if params.get("submit"):
                        await locator.press("Enter")
                    await page.wait_for_timeout(params.get("wait_ms", 400))
                    base_data["value_length"] = len(str(value or ""))
                    return await self._finalize_browser_result(
                        page,
                        action="act",
                        base_data=base_data,
                        include_screenshot=bool(params.get("include_screenshot", False)),
                        full_page=bool(params.get("full_page", False)),
                        include_snapshot=bool(params.get("include_snapshot", False)),
                    )

                if subaction == "wait_for":
                    timeout_ms = max(100, min(int(params.get("timeout_ms", 5000) or 5000), 30000))
                    state = str(params.get("state", "visible") or "visible").strip().lower()
                    load_state = str(params.get("load_state", "") or "").strip().lower()
                    if load_state:
                        await page.wait_for_load_state(load_state, timeout=timeout_ms)
                    if locator is not None:
                        await locator.wait_for(state=state, timeout=timeout_ms)
                    elif params.get("url_contains"):
                        expected = str(params.get("url_contains") or "").strip()
                        await page.wait_for_url(f"**{expected}**", timeout=timeout_ms)
                        base_data["target"] = f"url_contains={expected}"
                    elif params.get("text"):
                        expected = str(params.get("text") or "").strip()
                        await page.get_by_text(expected, exact=bool(params.get("exact", False))).wait_for(state=state, timeout=timeout_ms)
                        base_data["target"] = f"text={expected}"
                    else:
                        return PluginResult(success=False, error="act(wait_for) requires ref/target/selector/text/url_contains/load_state")
                    base_data["state"] = state
                    base_data["timeout_ms"] = timeout_ms
                    return await self._finalize_browser_result(
                        page,
                        action="act",
                        base_data=base_data,
                        include_screenshot=bool(params.get("include_screenshot", False)),
                        full_page=bool(params.get("full_page", False)),
                        include_snapshot=bool(params.get("include_snapshot", False)),
                    )

                if subaction == "press":
                    key = str(params.get("key", "") or "").strip()
                    if not key:
                        return PluginResult(success=False, error="act(press) requires a key")
                    if locator is not None:
                        await locator.press(key)
                    else:
                        await page.keyboard.press(key)
                    await page.wait_for_timeout(params.get("wait_ms", 500))
                    base_data["key"] = key
                    base_data["keys_count"] = 1
                    return await self._finalize_browser_result(
                        page,
                        action="act",
                        base_data=base_data,
                        include_screenshot=True,
                        full_page=bool(params.get("full_page", False)),
                        include_snapshot=bool(params.get("include_snapshot", False)),
                    )

                if subaction == "press_sequence":
                    keys = params.get("keys", [])
                    if isinstance(keys, str):
                        keys = [item.strip() for item in keys.split(",") if item.strip()]
                    if not isinstance(keys, list) or not keys:
                        return PluginResult(success=False, error="act(press_sequence) requires a non-empty keys array")
                    if locator is not None:
                        try:
                            await locator.focus()
                        except Exception:
                            pass
                    repeat = max(1, min(int(params.get("repeat", 1) or 1), 20))
                    interval_ms = max(0, min(int(params.get("interval_ms", 180) or 180), 5000))
                    normalized_keys: List[str] = []
                    for _ in range(repeat):
                        for raw_key in keys:
                            key = str(raw_key or "").strip()
                            if not key:
                                continue
                            normalized_keys.append(key)
                            await page.keyboard.press(key)
                            if interval_ms > 0:
                                await page.wait_for_timeout(interval_ms)
                    await page.wait_for_timeout(params.get("wait_ms", 500))
                    base_data["keys"] = normalized_keys[:40]
                    base_data["keys_count"] = len(normalized_keys)
                    return await self._finalize_browser_result(
                        page,
                        action="act",
                        base_data=base_data,
                        include_screenshot=True,
                        full_page=bool(params.get("full_page", False)),
                        include_snapshot=bool(params.get("include_snapshot", False)),
                    )

                return PluginResult(success=False, error=f"Unknown act subaction: {subaction}")

            if action == "click":
                locator, target = await self._resolve_locator(page, params)
                if locator is None:
                    # v6.4.52-C: actionable error. Previously chat agents
                    # (kimi especially) would send `{"action":"click"}` with
                    # no selector then get stuck because the error message
                    # didn't tell them what to do next. Now we attach a
                    # compact list of up to 8 clickable elements from the
                    # current page so the model can pick a concrete target.
                    try:
                        _candidates = await self._page_snapshot(page, limit=40)
                    except Exception:
                        _candidates = []
                    _suggest_lines: List[str] = []
                    if isinstance(_candidates, list):
                        for _c in _candidates:
                            if not isinstance(_c, dict):
                                continue
                            _role = str(_c.get("role") or "").lower()
                            if _role not in ("button", "link", "menuitem", "tab", "option",
                                              "checkbox", "radio", "combobox"):
                                continue
                            _ref = _c.get("ref") or _c.get("index")
                            _text = (_c.get("text") or _c.get("name") or "")[:60]
                            if _ref is not None and _text:
                                _suggest_lines.append(f"  - ref={_ref}  role={_role}  text={_text!r}")
                            if len(_suggest_lines) >= 8:
                                break
                    _msg = (
                        "click action requires a ref/selector/text/role/label/placeholder. "
                        "Tips: call observe first to get the page snapshot, then use click "
                        "with `ref: N` (N from the snapshot) OR `text: '按钮文字'`."
                    )
                    if _suggest_lines:
                        _msg += "\n\nTop clickable elements on current page:\n" + "\n".join(_suggest_lines)
                    return PluginResult(success=False, error=_msg)
                if self._cursor_overlay_enabled(context):
                    await self._ensure_cursor_overlay(page)
                    cx, cy = await self._bbox_center(locator)
                    if cx is not None and cy is not None:
                        await self._visualize_cursor_move(page, cx, cy, "click")
                        await page.wait_for_timeout(120)
                        await self._visualize_cursor_click(page, cx, cy, "left")
                await locator.click(timeout=int(params.get("timeout_ms", 5000) or 5000))
                await page.wait_for_timeout(params.get("wait_ms", 800))
                return await self._finalize_browser_result(
                    page,
                    action="click",
                    base_data={"target": target},
                    include_screenshot=True,
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "fill":
                locator, target = await self._resolve_locator(page, params)
                value = params.get("value", "")
                if locator is None:
                    return PluginResult(success=False, error="fill action requires a ref/selector/text/label/placeholder that exists")
                await locator.fill(value)
                if params.get("submit"):
                    await locator.press("Enter")
                await page.wait_for_timeout(params.get("wait_ms", 400))
                return await self._finalize_browser_result(
                    page,
                    action="fill",
                    base_data={"filled": target, "value_length": len(str(value or ""))},
                    include_screenshot=bool(params.get("include_screenshot", False)),
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "extract":
                selector = str(params.get("selector", "") or "").strip()
                mode = str(params.get("mode", "auto") or "auto").strip().lower()
                snapshot = await self._page_snapshot(page, limit=max(1, min(int(params.get("limit", 20) or 20), 40)))
                text = ""
                if selector and mode not in {"structured", "summary"}:
                    text = await page.text_content(selector) or ""
                else:
                    text = str(snapshot.get("body_text", "") or "")
                    selector = selector or "body"
                headings = await page.evaluate(
                    """
() => Array.from(document.querySelectorAll('h1, h2, h3'))
  .map((el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
  .filter(Boolean)
  .slice(0, 12)
                    """
                )
                structured_mode = not params.get("selector") or mode in {"structured", "summary"}
                return PluginResult(success=True, data={
                    "text": (text or "")[:5000],
                    "url": page.url,
                    "selector": selector,
                    "mode": "structured" if structured_mode else "selector",
                    "observation": self._summarize_snapshot(snapshot, goal=str(params.get("goal") or "").strip()),
                    "headings": headings if isinstance(headings, list) else [],
                    "interactive_refs": self._snapshot_ref_preview(snapshot, limit=10),
                    "requested_fields": params.get("fields", []),
                    "snapshot": snapshot,
                    **self._diagnostics_summary(),
                })

            if action == "scroll":
                direction = params.get("direction", "down")
                amount = int(params.get("amount", 500))
                delta = amount if direction == "down" else -amount
                before_metrics = await self._get_scroll_metrics(page)
                # v6.0: edge-glow hint so users see the AI scrolling
                if self._cursor_overlay_enabled():
                    try:
                        await self._ensure_cursor_overlay(page)
                        await self._visualize_scroll(page, str(direction))
                    except Exception:
                        pass
                await page.mouse.wheel(0, delta)
                await page.wait_for_timeout(600)
                after_metrics = await self._get_scroll_metrics(page)
                scroll_meta = self._scroll_metadata(
                    after_metrics,
                    direction=direction,
                    previous_scroll_y=int((before_metrics or {}).get("scrollY", 0) or 0),
                )
                return await self._finalize_browser_result(
                    page,
                    action="scroll",
                    base_data={
                        "direction": direction,
                        "amount": amount,
                        **scroll_meta,
                    },
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "record_scroll":
                amount = max(120, min(int(params.get("amount", 500) or 500), 2400))
                max_steps = max(2, min(int(params.get("max_steps", 12) or 12), 40))
                delay_ms = max(80, min(int(params.get("delay_ms", 220) or 220), 5000))
                metrics = await self._get_scroll_metrics(page)
                previous_scroll_y = int((metrics or {}).get("scrollY", 0) or 0)
                frame_bytes: List[bytes] = []
                frame_positions: List[int] = []

                def _capture_allowed(scroll_y: int) -> bool:
                    return not frame_positions or frame_positions[-1] != scroll_y

                for _ in range(max_steps):
                    scroll_y = int((metrics or {}).get("scrollY", 0) or 0)
                    if _capture_allowed(scroll_y):
                        frame_bytes.append(await page.screenshot(full_page=False))
                        frame_positions.append(scroll_y)
                    scroll_meta = self._scroll_metadata(metrics, direction="down", previous_scroll_y=previous_scroll_y)
                    if bool(scroll_meta.get("at_bottom")) or scroll_meta.get("is_scrollable") is False:
                        break
                    previous_scroll_y = scroll_y
                    await page.mouse.wheel(0, amount)
                    await page.wait_for_timeout(delay_ms)
                    next_metrics = await self._get_scroll_metrics(page)
                    next_scroll_y = int((next_metrics or {}).get("scrollY", 0) or 0)
                    metrics = next_metrics
                    if next_scroll_y == scroll_y:
                        break

                final_scroll_y = int((metrics or {}).get("scrollY", 0) or 0)
                if _capture_allowed(final_scroll_y):
                    frame_bytes.append(await page.screenshot(full_page=False))
                    frame_positions.append(final_scroll_y)

                scroll_meta = self._scroll_metadata(metrics, direction="down", previous_scroll_y=previous_scroll_y)
                artifact_dir = self._browser_artifact_dir(context)
                stamp = int(time.time() * 1000)
                gif_path = artifact_dir / f"scroll_record_{stamp}.gif"
                png_path = artifact_dir / f"scroll_record_{stamp}_last.png"
                saved_gif = self._write_scroll_gif(frame_bytes, output_path=gif_path, duration_ms=delay_ms)
                try:
                    if frame_bytes:
                        png_path.write_bytes(frame_bytes[-1])
                except Exception:
                    pass

                finalized = await self._finalize_browser_result(
                    page,
                    action="record_scroll",
                    base_data={
                        "amount": amount,
                        "max_steps": max_steps,
                        "frame_count": len(frame_bytes),
                        "recorded_to": str(gif_path if saved_gif else png_path),
                        **scroll_meta,
                    },
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )
                data = dict(finalized.data or {})
                artifacts = list(finalized.artifacts or [])
                if saved_gif and gif_path.exists():
                    artifacts.append({"type": "gif", "path": str(gif_path)})
                elif png_path.exists():
                    artifacts.append({"type": "image", "path": str(png_path)})
                return PluginResult(success=True, data=data, artifacts=artifacts)

            if action == "press":
                key = str(params.get("key", "") or "").strip()
                if not key:
                    return PluginResult(success=False, error="press action requires a key")
                locator, target = await self._resolve_locator(page, params)
                if self._cursor_overlay_enabled(context):
                    await self._ensure_cursor_overlay(page)
                    await self._visualize_cursor_key(page, key)
                if locator is not None:
                    await locator.press(key)
                else:
                    await page.keyboard.press(key)
                await page.wait_for_timeout(params.get("wait_ms", 500))
                return await self._finalize_browser_result(
                    page,
                    action="press",
                    base_data={"key": key, "target": target or None, "keys_count": 1},
                    include_screenshot=True,
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "press_sequence":
                keys = params.get("keys", [])
                if isinstance(keys, str):
                    keys = [item.strip() for item in keys.split(",") if item.strip()]
                if not isinstance(keys, list) or not keys:
                    return PluginResult(success=False, error="press_sequence action requires a non-empty keys array")
                locator, target = await self._resolve_locator(page, params)
                if locator is not None:
                    try:
                        await locator.focus()
                    except Exception:
                        pass
                repeat = max(1, min(int(params.get("repeat", 1) or 1), 20))
                interval_ms = max(0, min(int(params.get("interval_ms", 180) or 180), 5000))
                normalized_keys: List[str] = []
                for _ in range(repeat):
                    for raw_key in keys:
                        key = str(raw_key or "").strip()
                        if not key:
                            continue
                        normalized_keys.append(key)
                        await page.keyboard.press(key)
                        if interval_ms > 0:
                            await page.wait_for_timeout(interval_ms)
                await page.wait_for_timeout(params.get("wait_ms", 500))
                return await self._finalize_browser_result(
                    page,
                    action="press_sequence",
                    base_data={
                        "keys": normalized_keys[:40],
                        "keys_count": len(normalized_keys),
                        "target": target or None,
                    },
                    include_screenshot=True,
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "wait_for":
                timeout_ms = max(100, min(int(params.get("timeout_ms", 5000) or 5000), 30000))
                state = str(params.get("state", "visible") or "visible").strip().lower()
                load_state = str(params.get("load_state", "") or "").strip().lower()
                if load_state:
                    await page.wait_for_load_state(load_state, timeout=timeout_ms)
                locator, target = await self._resolve_locator(page, params)
                if locator is not None:
                    await locator.wait_for(state=state, timeout=timeout_ms)
                elif params.get("url_contains"):
                    expected = str(params.get("url_contains") or "").strip()
                    await page.wait_for_url(f"**{expected}**", timeout=timeout_ms)
                    target = f"url_contains={expected}"
                elif params.get("text"):
                    expected = str(params.get("text") or "").strip()
                    await page.get_by_text(expected, exact=bool(params.get("exact", False))).wait_for(state=state, timeout=timeout_ms)
                    target = f"text={expected}"
                else:
                    return PluginResult(success=False, error="wait_for action requires ref/selector/text/url_contains/load_state")
                return await self._finalize_browser_result(
                    page,
                    action="wait_for",
                    base_data={"target": target, "state": state, "timeout_ms": timeout_ms},
                    include_screenshot=bool(params.get("include_screenshot", False)),
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            # ── v5.8.4 new actions (browser-use parity +) ─────────────────
            if action == "find":
                # Full-page text search including off-screen content. No LLM call,
                # no token cost — pure JS grep. Mirrors browser-use search_page.
                query = str(params.get("query") or params.get("text") or "").strip()
                if not query:
                    return PluginResult(success=False, error="find action requires a query")
                case_sensitive = bool(params.get("case_sensitive", False))
                max_hits = max(1, min(int(params.get("limit", 20) or 20), 100))
                hits = await page.evaluate(
                    """
({q, cs, limit}) => {
  const needle = cs ? q : q.toLowerCase();
  const results = [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode()) && results.length < limit) {
    const raw = String(node.nodeValue || '');
    const hay = cs ? raw : raw.toLowerCase();
    const idx = hay.indexOf(needle);
    if (idx < 0) continue;
    const host = node.parentElement;
    if (!host) continue;
    const rect = host.getBoundingClientRect();
    const start = Math.max(0, idx - 40);
    const end = Math.min(raw.length, idx + needle.length + 60);
    const snippet = raw.slice(start, end).replace(/\\s+/g, ' ').trim();
    results.push({
      snippet: snippet,
      tag: host.tagName.toLowerCase(),
      in_viewport: rect.top >= 0 && rect.bottom <= (window.innerHeight || 0) && rect.height > 0,
      scrollY_needed: Math.max(0, Math.round(rect.top + (window.scrollY || 0) - 80)),
    });
  }
  return results;
}
                    """,
                    {"q": query, "cs": case_sensitive, "limit": max_hits},
                )
                return PluginResult(success=True, data={
                    "action": "find",
                    "url": page.url,
                    "query": query,
                    "match_count": len(hits) if isinstance(hits, list) else 0,
                    "matches": hits if isinstance(hits, list) else [],
                    **self._diagnostics_summary(),
                })

            if action == "hover":
                locator, target = await self._resolve_locator(page, params)
                if locator is None:
                    return PluginResult(success=False, error="hover action requires a resolvable target")
                await locator.hover(timeout=int(params.get("timeout_ms", 5000) or 5000))
                await page.wait_for_timeout(params.get("wait_ms", 400))
                return await self._finalize_browser_result(
                    page,
                    action="hover",
                    base_data={"target": target},
                    include_screenshot=bool(params.get("include_screenshot", True)),
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "select":
                # Dropdown / <select> handling. Returns options when no value given.
                locator, target = await self._resolve_locator(page, params)
                if locator is None:
                    return PluginResult(success=False, error="select action requires a resolvable <select> target")
                value = params.get("value")
                label = params.get("label")
                if value is None and label is None:
                    # Enumerate options for the LLM to choose from.
                    options = await locator.evaluate(
                        """
(el) => {
  if (!el || el.tagName !== 'SELECT') return [];
  return Array.from(el.options).map((o, i) => ({
    index: i,
    value: o.value,
    label: String(o.label || o.text || '').trim().slice(0, 120),
    selected: o.selected,
    disabled: o.disabled,
  }));
}
                        """
                    )
                    return PluginResult(success=True, data={
                        "action": "select",
                        "mode": "list_options",
                        "target": target,
                        "options": options if isinstance(options, list) else [],
                    })
                kwargs: Dict[str, Any] = {}
                if value is not None:
                    kwargs["value"] = str(value)
                if label is not None:
                    kwargs["label"] = str(label)
                await locator.select_option(**kwargs)
                await page.wait_for_timeout(params.get("wait_ms", 300))
                return await self._finalize_browser_result(
                    page,
                    action="select",
                    base_data={"target": target, "value": value, "label": label},
                    include_screenshot=bool(params.get("include_screenshot", False)),
                    full_page=bool(params.get("full_page", False)),
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "upload":
                locator, target = await self._resolve_locator(page, params)
                raw_files = params.get("files") or params.get("path") or params.get("paths")
                if isinstance(raw_files, str):
                    files_list = [raw_files]
                elif isinstance(raw_files, list):
                    files_list = [str(p) for p in raw_files if str(p).strip()]
                else:
                    files_list = []
                if not files_list:
                    return PluginResult(success=False, error="upload action requires files/path/paths")
                if locator is None:
                    # Fallback: set_input_files on first visible <input type=file>
                    locator = page.locator('input[type="file"]').first
                await locator.set_input_files(files_list)
                await page.wait_for_timeout(params.get("wait_ms", 400))
                return await self._finalize_browser_result(
                    page,
                    action="upload",
                    base_data={"target": target, "file_count": len(files_list)},
                    include_screenshot=bool(params.get("include_screenshot", False)),
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "new_tab":
                if not self._context:
                    return PluginResult(success=False, error="browser context not ready for new_tab")
                new_page = await self._context.new_page()
                self._page = new_page
                self._bind_page_diagnostics(new_page)
                url_to_open = str(params.get("url") or "").strip()
                if url_to_open:
                    await new_page.goto(url_to_open, wait_until="domcontentloaded")
                pages_count = len(self._context.pages)
                return await self._finalize_browser_result(
                    new_page,
                    action="new_tab",
                    base_data={"tab_count": pages_count, "title": await new_page.title()},
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", True)),
                )

            if action == "switch_tab":
                if not self._context:
                    return PluginResult(success=False, error="browser context not ready for switch_tab")
                pages = self._context.pages
                idx_raw = params.get("index")
                if idx_raw is None:
                    # Enumerate tabs
                    tabs = [{"index": i, "url": p.url, "title": await p.title()} for i, p in enumerate(pages)]
                    return PluginResult(success=True, data={
                        "action": "switch_tab",
                        "mode": "list_tabs",
                        "tab_count": len(pages),
                        "tabs": tabs,
                    })
                try:
                    idx = max(0, min(int(idx_raw), len(pages) - 1))
                except Exception:
                    return PluginResult(success=False, error="switch_tab index must be an integer")
                target_page = pages[idx]
                await target_page.bring_to_front()
                self._page = target_page
                self._bind_page_diagnostics(target_page)
                return await self._finalize_browser_result(
                    target_page,
                    action="switch_tab",
                    base_data={"index": idx, "title": await target_page.title()},
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", True)),
                )

            if action == "close_tab":
                if not self._context:
                    return PluginResult(success=False, error="browser context not ready for close_tab")
                pages = self._context.pages
                if len(pages) <= 1:
                    return PluginResult(success=False, error="cannot close the last remaining tab")
                idx_raw = params.get("index")
                try:
                    idx = int(idx_raw) if idx_raw is not None else pages.index(self._page)
                except Exception:
                    idx = 0
                idx = max(0, min(idx, len(pages) - 1))
                closing = pages[idx]
                await closing.close()
                remaining = self._context.pages
                if remaining:
                    self._page = remaining[-1]
                    self._bind_page_diagnostics(self._page)
                return PluginResult(success=True, data={
                    "action": "close_tab",
                    "closed_index": idx,
                    "tab_count": len(remaining),
                })

            if action == "evaluate":
                # Execute arbitrary JS in page context. Harness-gated: the agent
                # harness (browser tool description) decides whether to expose this.
                script = str(params.get("script") or params.get("expression") or "").strip()
                if not script:
                    return PluginResult(success=False, error="evaluate action requires a script")
                max_len = max(64, min(int(params.get("max_len", 4000) or 4000), 20000))
                try:
                    result = await page.evaluate(script)
                except Exception as exc:
                    return PluginResult(success=False, error=f"evaluate failed: {str(exc)[:300]}")
                raw_result = result
                try:
                    result_text = json.dumps(result, ensure_ascii=False, default=str)
                except Exception:
                    result_text = str(result)
                return PluginResult(success=True, data={
                    "action": "evaluate",
                    "url": page.url,
                    "result": result_text[:max_len],
                    "result_raw": raw_result if isinstance(raw_result, (dict, list, str, int, float, bool)) or raw_result is None else None,
                })

            if action == "close_popups":
                # Auto-dismiss common overlay patterns: cookie banners, modals,
                # newsletter popups. Runs entirely in page-side JS.
                dismissed = await page.evaluate(
                    """
() => {
  const candidates = [];
  const selectors = [
    '[aria-label*="close" i]',
    '[aria-label*="dismiss" i]',
    'button[class*="close" i]',
    'button[class*="dismiss" i]',
    '[data-testid*="close" i]',
    '[id*="cookie" i] button',
    '[class*="cookie" i] button',
    '[class*="consent" i] button',
    '[class*="modal" i] button[class*="close" i]',
    '[aria-label*="关闭" i]',
    'button:has-text("Accept")',
    'button:has-text("同意")',
  ];
  const clicked = [];
  for (const sel of selectors) {
    try {
      document.querySelectorAll(sel).forEach((el) => {
        if (clicked.length >= 6) return;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return;
        try { el.click(); clicked.push(sel); } catch (e) {}
      });
    } catch (e) {}
    if (clicked.length >= 6) break;
  }
  // Also ESC for stray modal overlays
  try { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); } catch (e) {}
  return clicked;
}
                    """
                )
                await page.wait_for_timeout(400)
                return await self._finalize_browser_result(
                    page,
                    action="close_popups",
                    base_data={
                        "dismissed_selectors": dismissed if isinstance(dismissed, list) else [],
                        "dismissed_count": len(dismissed) if isinstance(dismissed, list) else 0,
                    },
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "network_idle":
                timeout_ms = max(500, min(int(params.get("timeout_ms", 8000) or 8000), 60000))
                try:
                    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                    idle_ok = True
                except Exception as exc:
                    idle_ok = False
                return await self._finalize_browser_result(
                    page,
                    action="network_idle",
                    base_data={"idle": idle_ok, "timeout_ms": timeout_ms},
                    include_screenshot=False,
                    full_page=False,
                    include_snapshot=False,
                )

            # ── v5.8.5 精确鼠标 / 键盘 / 游戏原子操作 ─────────────────
            if action == "mouse_click":
                # Pixel-perfect click at (x, y). Supports canvas games, custom UIs,
                # and anywhere the DOM selector can't reach (e.g. WebGL viewports).
                try:
                    x = float(params.get("x"))
                    y = float(params.get("y"))
                except (TypeError, ValueError):
                    return PluginResult(success=False, error="mouse_click requires numeric x and y")
                button = str(params.get("button", "left") or "left").strip().lower()
                if button not in {"left", "right", "middle"}:
                    button = "left"
                click_count = max(1, min(int(params.get("click_count", 1) or 1), 3))
                delay_ms = max(0, min(int(params.get("delay_ms", 0) or 0), 2000))
                canvas_target = params.get("canvas")  # optional selector to resolve canvas origin
                cx, cy = x, y
                canvas_rect = None
                if canvas_target:
                    try:
                        canvas_rect = await page.evaluate(
                            """
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}
                            """,
                            str(canvas_target),
                        )
                    except Exception:
                        canvas_rect = None
                    if canvas_rect:
                        cx = float(canvas_rect.get("left", 0)) + x
                        cy = float(canvas_rect.get("top", 0)) + y
                # v5.8.5: visible AI-cursor so the user watches the click happen.
                if self._cursor_overlay_enabled(context):
                    await self._ensure_cursor_overlay(page)
                    await self._visualize_cursor_move(page, cx, cy, "click")
                    await page.wait_for_timeout(120)
                    await self._visualize_cursor_click(page, cx, cy, button)
                await page.mouse.click(cx, cy, button=button, click_count=click_count, delay=delay_ms)
                await page.wait_for_timeout(params.get("wait_ms", 400))
                return await self._finalize_browser_result(
                    page,
                    action="mouse_click",
                    base_data={
                        "x": cx, "y": cy,
                        "rel_x": x if canvas_target else None,
                        "rel_y": y if canvas_target else None,
                        "canvas": canvas_target or None,
                        "button": button,
                        "click_count": click_count,
                    },
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "mouse_move":
                try:
                    x = float(params.get("x"))
                    y = float(params.get("y"))
                except (TypeError, ValueError):
                    return PluginResult(success=False, error="mouse_move requires numeric x and y")
                steps = max(1, min(int(params.get("steps", 10) or 10), 60))
                if self._cursor_overlay_enabled(context):
                    await self._ensure_cursor_overlay(page)
                    await self._visualize_cursor_move(page, x, y, "move")
                await page.mouse.move(x, y, steps=steps)
                await page.wait_for_timeout(params.get("wait_ms", 150))
                return await self._finalize_browser_result(
                    page,
                    action="mouse_move",
                    base_data={"x": x, "y": y, "steps": steps},
                    include_screenshot=bool(params.get("include_screenshot", False)),
                    full_page=False,
                    include_snapshot=False,
                )

            if action == "mouse_down":
                try:
                    x = params.get("x")
                    y = params.get("y")
                    if x is not None and y is not None:
                        await page.mouse.move(float(x), float(y))
                except (TypeError, ValueError):
                    pass
                button = str(params.get("button", "left") or "left").strip().lower()
                if button not in {"left", "right", "middle"}:
                    button = "left"
                await page.mouse.down(button=button)
                return PluginResult(success=True, data={"action": "mouse_down", "button": button})

            if action == "mouse_up":
                try:
                    x = params.get("x")
                    y = params.get("y")
                    if x is not None and y is not None:
                        await page.mouse.move(float(x), float(y))
                except (TypeError, ValueError):
                    pass
                button = str(params.get("button", "left") or "left").strip().lower()
                if button not in {"left", "right", "middle"}:
                    button = "left"
                await page.mouse.up(button=button)
                return PluginResult(success=True, data={"action": "mouse_up", "button": button})

            if action == "drag":
                # Full drag (down → move → up) from (from_x, from_y) to (to_x, to_y).
                # Handles canvas drag, slider drag, drag-and-drop UI.
                try:
                    fx = float(params.get("from_x", params.get("x1")))
                    fy = float(params.get("from_y", params.get("y1")))
                    tx = float(params.get("to_x", params.get("x2")))
                    ty = float(params.get("to_y", params.get("y2")))
                except (TypeError, ValueError):
                    return PluginResult(success=False, error="drag requires numeric from_x/from_y and to_x/to_y")
                steps = max(2, min(int(params.get("steps", 20) or 20), 120))
                button = str(params.get("button", "left") or "left").strip().lower()
                if self._cursor_overlay_enabled(context):
                    await self._ensure_cursor_overlay(page)
                    await self._visualize_cursor_move(page, fx, fy, "drag")
                    await self._visualize_cursor_click(page, fx, fy, button)
                await page.mouse.move(fx, fy)
                await page.mouse.down(button=button)
                await page.mouse.move(tx, ty, steps=steps)
                if self._cursor_overlay_enabled(context):
                    await self._visualize_cursor_move(page, tx, ty, "drop")
                await page.wait_for_timeout(params.get("hold_ms", 120))
                await page.mouse.up(button=button)
                if self._cursor_overlay_enabled(context):
                    await self._visualize_cursor_click(page, tx, ty, button)
                await page.wait_for_timeout(params.get("wait_ms", 400))
                return await self._finalize_browser_result(
                    page,
                    action="drag",
                    base_data={
                        "from": {"x": fx, "y": fy},
                        "to": {"x": tx, "y": ty},
                        "steps": steps,
                        "button": button,
                    },
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "mouse_delta":
                # v7.15 (maintainer 2026-04-28) — Pointer Lock mouselook simulation.
                # Playwright `page.mouse.move(x, y)` does NOT generate non-zero
                # `movementX/movementY` when Pointer Lock is engaged (the FPS
                # camera receives 0 deltas, camera doesn't rotate). Per web.dev
                # Pointer Lock guide + GitHub research, the only reliable
                # browser-side approach is to construct a MouseEvent and use
                # Object.defineProperty to override movementX/Y getters before
                # dispatching. This action lets the reviewer/tester rotate the
                # FPS/TPS camera in fast cycles without leaving Pointer Lock.
                try:
                    dx = float(params.get("dx", params.get("delta_x", 0)) or 0)
                    dy = float(params.get("dy", params.get("delta_y", 0)) or 0)
                except (TypeError, ValueError):
                    return PluginResult(success=False, error="mouse_delta requires numeric dx/dy")
                # Clamp to sane bounds (FPS sensitivity in browser ≤ 200 / event)
                dx = max(-500.0, min(500.0, dx))
                dy = max(-500.0, min(500.0, dy))
                target_selector = str(params.get("target", "") or "").strip()
                try:
                    await page.evaluate(
                        """
                        ([dx, dy, sel]) => {
                          const evt = new MouseEvent('mousemove', {
                            bubbles: true, cancelable: true, view: window,
                          });
                          Object.defineProperty(evt, 'movementX', {value: dx, enumerable: true});
                          Object.defineProperty(evt, 'movementY', {value: dy, enumerable: true});
                          const target = sel ? document.querySelector(sel) : document;
                          (target || document).dispatchEvent(evt);
                          return { dispatched: true, dx, dy, target: target?.tagName || 'document' };
                        }
                        """,
                        [dx, dy, target_selector],
                    )
                except Exception as exc:
                    return PluginResult(success=False, error=f"mouse_delta dispatch failed: {exc}")
                await page.wait_for_timeout(int(params.get("wait_ms", 80)))
                return await self._finalize_browser_result(
                    page,
                    action="mouse_delta",
                    base_data={"dx": dx, "dy": dy, "target": target_selector or "document"},
                    include_screenshot=bool(params.get("include_screenshot", False)),
                    include_snapshot=False,
                )

            if action == "wheel":
                # Scroll wheel at current mouse position. Useful for canvas scroll,
                # map zoom, and custom scroll containers that don't respond to page wheel.
                try:
                    dx = float(params.get("dx", 0) or 0)
                    dy = float(params.get("dy", params.get("delta", 500)) or 500)
                except (TypeError, ValueError):
                    return PluginResult(success=False, error="wheel requires numeric dx/dy")
                if params.get("x") is not None and params.get("y") is not None:
                    try:
                        await page.mouse.move(float(params["x"]), float(params["y"]))
                    except Exception:
                        pass
                await page.mouse.wheel(dx, dy)
                await page.wait_for_timeout(params.get("wait_ms", 400))
                return await self._finalize_browser_result(
                    page,
                    action="wheel",
                    base_data={"dx": dx, "dy": dy},
                    include_screenshot=bool(params.get("include_screenshot", False)),
                    full_page=False,
                    include_snapshot=False,
                )

            if action == "key_down":
                key = str(params.get("key", "") or "").strip()
                if not key:
                    return PluginResult(success=False, error="key_down requires key")
                if self._cursor_overlay_enabled(context):
                    await self._ensure_cursor_overlay(page)
                    await self._visualize_cursor_key(page, f"{key} ↓")
                await page.keyboard.down(key)
                return PluginResult(success=True, data={"action": "key_down", "key": key})

            if action == "key_up":
                key = str(params.get("key", "") or "").strip()
                if not key:
                    return PluginResult(success=False, error="key_up requires key")
                if self._cursor_overlay_enabled(context):
                    await self._visualize_cursor_key(page, f"{key} ↑")
                await page.keyboard.up(key)
                return PluginResult(success=True, data={"action": "key_up", "key": key})

            if action == "key_hold":
                # Press and hold a key for duration_ms. Perfect for games where
                # movement requires continuous keypress (WASD hold to walk).
                key = str(params.get("key", "") or "").strip()
                if not key:
                    return PluginResult(success=False, error="key_hold requires key")
                duration_ms = max(50, min(int(params.get("duration_ms", 500) or 500), 10000))
                if self._cursor_overlay_enabled(context):
                    await self._ensure_cursor_overlay(page)
                    await self._visualize_cursor_key(page, f"{key} ({duration_ms}ms)")
                await page.keyboard.down(key)
                await page.wait_for_timeout(duration_ms)
                await page.keyboard.up(key)
                await page.wait_for_timeout(params.get("wait_ms", 200))
                return await self._finalize_browser_result(
                    page,
                    action="key_hold",
                    base_data={"key": key, "duration_ms": duration_ms},
                    include_screenshot=bool(params.get("include_screenshot", True)),
                    full_page=False,
                    include_snapshot=False,
                )

            if action == "type_text":
                # Type a string with optional per-char delay (for anti-bot detection).
                text_to_type = str(params.get("text", "") or "")
                if not text_to_type:
                    return PluginResult(success=False, error="type_text requires text")
                delay_ms = max(0, min(int(params.get("delay_ms", 40) or 40), 500))
                await page.keyboard.type(text_to_type, delay=delay_ms)
                await page.wait_for_timeout(params.get("wait_ms", 200))
                return await self._finalize_browser_result(
                    page,
                    action="type_text",
                    base_data={"char_count": len(text_to_type), "delay_ms": delay_ms},
                    include_screenshot=bool(params.get("include_screenshot", False)),
                    full_page=False,
                    include_snapshot=False,
                )

            if action == "macro":
                # Execute a sequence of sub-actions in one call. Minimizes
                # LLM round-trips for multi-step game/UI combos.
                # Each step is a dict with its own "action" and params.
                steps_raw = params.get("steps") or params.get("sequence") or []
                if not isinstance(steps_raw, list) or not steps_raw:
                    return PluginResult(success=False, error="macro requires steps: [{action,...}, ...]")
                max_macro_steps = max(1, min(int(params.get("max_steps", 20) or 20), 40))
                executed: List[Dict[str, Any]] = []
                errors: List[str] = []
                for i, step_raw in enumerate(steps_raw[:max_macro_steps]):
                    if not isinstance(step_raw, dict):
                        errors.append(f"step {i}: not a dict")
                        continue
                    step_params = dict(step_raw)
                    sub_action = str(step_params.pop("action", "") or "").strip().lower()
                    if not sub_action or sub_action == "macro":
                        errors.append(f"step {i}: invalid action '{sub_action}'")
                        continue
                    if sub_action == "wait" or sub_action == "sleep":
                        ms = max(1, min(int(step_params.get("ms", step_params.get("duration_ms", 500)) or 500), 15000))
                        await page.wait_for_timeout(ms)
                        executed.append({"step": i, "action": "wait", "ms": ms, "success": True})
                        continue
                    step_params["action"] = sub_action
                    # No screenshots per sub-step to keep artifact volume sane.
                    step_params.setdefault("include_screenshot", False)
                    step_params.setdefault("include_snapshot", False)
                    try:
                        sub_result = await self.execute(step_params, context=context)
                        executed.append({
                            "step": i,
                            "action": sub_action,
                            "success": bool(sub_result.success),
                            "error": (sub_result.error or "")[:200] if not sub_result.success else None,
                        })
                        if not sub_result.success:
                            errors.append(f"step {i} ({sub_action}): {sub_result.error}")
                            if params.get("stop_on_error", False):
                                break
                    except Exception as exc:
                        errors.append(f"step {i} ({sub_action}): {str(exc)[:200]}")
                        executed.append({"step": i, "action": sub_action, "success": False, "error": str(exc)[:200]})
                        if params.get("stop_on_error", False):
                            break
                # Final screenshot after macro completes — single visual proof.
                return await self._finalize_browser_result(
                    page,
                    action="macro",
                    base_data={
                        "executed_count": len(executed),
                        "error_count": len(errors),
                        "executed": executed,
                        "errors": errors[:10],
                    },
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "canvas_click":
                # Click inside a specific canvas element's local coord space.
                # Shortcut for the extremely common "click (100, 200) inside the game canvas" pattern.
                try:
                    rel_x = float(params.get("x"))
                    rel_y = float(params.get("y"))
                except (TypeError, ValueError):
                    return PluginResult(success=False, error="canvas_click requires numeric x and y")
                selector = str(params.get("selector", "canvas") or "canvas").strip()
                canvas_rect = await page.evaluate(
                    """
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height, tag: el.tagName.toLowerCase() };
}
                    """,
                    selector,
                )
                if not canvas_rect:
                    return PluginResult(success=False, error=f"canvas_click: no element matches '{selector}'")
                cx = float(canvas_rect["left"]) + rel_x
                cy = float(canvas_rect["top"]) + rel_y
                button = str(params.get("button", "left") or "left").strip().lower()
                click_count = max(1, min(int(params.get("click_count", 1) or 1), 3))
                if self._cursor_overlay_enabled(context):
                    await self._ensure_cursor_overlay(page)
                    await self._visualize_cursor_move(page, cx, cy, "canvas")
                    await page.wait_for_timeout(100)
                    await self._visualize_cursor_click(page, cx, cy, button)
                await page.mouse.click(cx, cy, button=button, click_count=click_count)
                await page.wait_for_timeout(params.get("wait_ms", 400))
                return await self._finalize_browser_result(
                    page,
                    action="canvas_click",
                    base_data={
                        "selector": selector,
                        "canvas": canvas_rect,
                        "rel_x": rel_x, "rel_y": rel_y,
                        "abs_x": cx, "abs_y": cy,
                        "button": button,
                        "click_count": click_count,
                    },
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=False,
                )

            if action == "screenshot_region":
                # Capture a bounded region instead of the whole viewport.
                try:
                    rx = float(params.get("x", 0) or 0)
                    ry = float(params.get("y", 0) or 0)
                    rw = float(params.get("w", params.get("width")))
                    rh = float(params.get("h", params.get("height")))
                except (TypeError, ValueError):
                    return PluginResult(success=False, error="screenshot_region requires x/y/w/h")
                try:
                    shot = await page.screenshot(clip={"x": rx, "y": ry, "width": rw, "height": rh})
                except Exception as exc:
                    return PluginResult(success=False, error=f"screenshot_region failed: {str(exc)[:200]}")
                b64 = base64.b64encode(shot).decode("utf-8")
                capture_path = self._write_browser_capture(shot, action="screenshot_region")
                artifacts: List[Dict[str, Any]] = [{"type": "image", "base64": b64}]
                data: Dict[str, Any] = {
                    "action": "screenshot_region",
                    "x": rx, "y": ry, "w": rw, "h": rh,
                    "url": page.url,
                }
                if capture_path is not None:
                    artifacts.append({"type": "image", "path": str(capture_path)})
                    data["capture_path"] = str(capture_path)
                return PluginResult(success=True, data=data, artifacts=artifacts)

            # v6.4.52 (maintainer 2026-04-23): 'screenshot' is a natural name
            # chat-agent models expect — alias it to a full-viewport grab.
            # Previously we only had 'snapshot' / 'screenshot_region' and
            # kimi's "action=screenshot" kept returning Unknown action,
            # pushing kimi to fall back to file_ops read. This caused the
            # read-only loop observed in Apr 23 11:13-11:14 sessions.
            if action == "screenshot":
                try:
                    shot = await page.screenshot(
                        full_page=bool(params.get("full_page", False)),
                    )
                except Exception as exc:
                    return PluginResult(success=False, error=f"screenshot failed: {str(exc)[:200]}")
                import base64 as _b64_ss
                b64 = _b64_ss.b64encode(shot).decode("utf-8")
                capture_path = self._write_browser_capture(shot, action="screenshot")
                artifacts: List[Dict[str, Any]] = [{"type": "image", "base64": b64}]
                data: Dict[str, Any] = {
                    "action": "screenshot",
                    "url": page.url,
                    "title": await page.title(),
                    "size_bytes": len(shot),
                }
                if capture_path is not None:
                    artifacts.append({"type": "image", "path": str(capture_path)})
                    data["capture_path"] = str(capture_path)
                return PluginResult(success=True, data=data, artifacts=artifacts)

            return PluginResult(success=False, error=f"Unknown action: {action}")
        except Exception as e:
            if trace_started:
                try:
                    await self._stop_trace_for_action(f"{action}_error", context)
                except Exception:
                    pass
            return PluginResult(success=False, error=str(e))

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["navigate", "observe", "act", "snapshot", "click", "fill", "extract", "scroll", "record_scroll", "press", "press_sequence", "wait_for", "find", "hover", "select", "upload", "new_tab", "switch_tab", "close_tab", "evaluate", "close_popups", "network_idle", "mouse_click", "mouse_move", "mouse_delta", "mouse_down", "mouse_up", "drag", "wheel", "key_down", "key_up", "key_hold", "type_text", "macro", "canvas_click", "screenshot_region"]},
                "url": {"type": "string", "description": "URL to navigate to before performing the action"},
                "goal": {"type": "string", "description": "High-level intent for observe/extract actions"},
                "target": {"type": "string", "description": "High-level semantic target for act/observe, e.g. 'Start Game' or 'email input'"},
                "subaction": {"type": "string", "description": "For act: click, fill, press, press_sequence, wait_for"},
                "ref": {"type": "string", "description": "Stable element reference from a prior browser snapshot, e.g. ref-3"},
                "selector": {"type": "string", "description": "CSS selector for click/fill/extract/wait_for"},
                "text": {"type": "string", "description": "Visible text target for click/fill/wait_for"},
                "role": {"type": "string", "description": "Accessible role for click/wait targeting, e.g. button, link, textbox"},
                "label": {"type": "string", "description": "Form label text for fill/click targeting"},
                "placeholder": {"type": "string", "description": "Input placeholder for fill targeting"},
                "nth": {"type": "integer", "description": "Optional zero-based locator index when multiple matches exist"},
                "exact": {"type": "boolean", "description": "Whether text/label matching should be exact"},
                "value": {"type": "string", "description": "Value for fill action"},
                "key": {"type": "string", "description": "Keyboard key for press action, e.g. ArrowUp, KeyW, Space, Enter"},
                "keys": {"type": "array", "items": {"type": "string"}, "description": "Key sequence for press_sequence, e.g. [\"ArrowRight\", \"ArrowRight\", \"Space\"]"},
                "repeat": {"type": "integer", "description": "How many times to repeat the press_sequence"},
                "interval_ms": {"type": "integer", "description": "Delay between keys for press_sequence"},
                "submit": {"type": "boolean", "description": "Press Enter after filling the field"},
                "url_contains": {"type": "string", "description": "Expected URL fragment for wait_for"},
                "state": {"type": "string", "description": "Desired wait_for state, e.g. visible, attached, hidden"},
                "load_state": {"type": "string", "description": "Optional Playwright load state for wait_for, e.g. load, domcontentloaded, networkidle"},
                "timeout_ms": {"type": "integer", "description": "Timeout for click/wait_for actions"},
                "wait_ms": {"type": "integer", "description": "Optional wait after click"},
                "max_steps": {"type": "integer", "description": "Maximum scroll steps for record_scroll"},
                "delay_ms": {"type": "integer", "description": "Delay between captured scroll steps for record_scroll"},
                "limit": {"type": "integer", "description": "Snapshot element limit"},
                "mode": {"type": "string", "description": "For extract/act helpers: auto, structured, summary, click, fill, wait"},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Optional high-level fields to extract from the page"},
                "include_snapshot": {"type": "boolean", "description": "Include structured page snapshot in action result"},
                "include_screenshot": {"type": "boolean", "description": "Capture screenshot for non-visual actions like fill/wait_for"},
                "screenshot": {"type": "boolean", "description": "Capture screenshot for snapshot action"},
                "full_page": {"type": "boolean", "description": "Capture a full-page screenshot when supported"},
                "query": {"type": "string", "description": "Search query for find action (grep-style)"},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive find"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "File paths for upload action"},
                "path": {"type": "string", "description": "Single file path for upload action"},
                "index": {"type": "integer", "description": "Tab index for switch_tab / close_tab"},
                "script": {"type": "string", "description": "JavaScript expression for evaluate action"},
                "max_len": {"type": "integer", "description": "Max evaluate result length in chars"},
                "x": {"type": "number", "description": "Pixel x-coordinate for mouse_click / mouse_move / screenshot_region / canvas_click"},
                "y": {"type": "number", "description": "Pixel y-coordinate for mouse_click / mouse_move / screenshot_region / canvas_click"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button"},
                "click_count": {"type": "integer", "description": "1 = single, 2 = double-click, 3 = triple-click"},
                "from_x": {"type": "number", "description": "Drag start x"},
                "from_y": {"type": "number", "description": "Drag start y"},
                "to_x": {"type": "number", "description": "Drag end x"},
                "to_y": {"type": "number", "description": "Drag end y"},
                "dx": {"type": "number", "description": "Wheel horizontal delta"},
                "dy": {"type": "number", "description": "Wheel vertical delta (positive = scroll down)"},
                "duration_ms": {"type": "integer", "description": "Duration for key_hold — keeps the key pressed N ms (e.g. WASD walk)"},
                "steps": {"type": "integer", "description": "Interpolation steps for mouse_move / drag (smoother movement)"},
                "canvas": {"type": "string", "description": "Optional canvas selector to treat (x,y) as relative to that canvas"},
                "w": {"type": "number", "description": "Region width for screenshot_region"},
                "h": {"type": "number", "description": "Region height for screenshot_region"},
                "macro_steps": {"type": "array", "items": {"type": "object"}, "description": "For macro: array of sub-action dicts"},
                "stop_on_error": {"type": "boolean", "description": "Stop macro at first error"}
            },
            "required": ["action"]
        }


# ─────────────────────────────────────────────
# 3. Source Fetch Plugin
# ─────────────────────────────────────────────
class _SourceFetchHTMLExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._title_depth = 0
        self._code_depth = 0
        self._title_chunks: List[str] = []
        self._text_chunks: List[str] = []
        self._code_chunks: List[str] = []
        self._current_code: List[str] = []
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        lower = str(tag or "").strip().lower()
        if lower in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if lower == "title":
            self._title_depth += 1
            return
        if lower in {"pre", "code"}:
            self._code_depth += 1
            return
        if lower == "a":
            href = ""
            for key, value in attrs or []:
                if str(key or "").strip().lower() == "href" and value:
                    href = str(value).strip()
                    break
            if href and href not in self.links:
                self.links.append(href)

    def handle_endtag(self, tag: str):
        lower = str(tag or "").strip().lower()
        if lower in {"script", "style", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if lower == "title":
            self._title_depth = max(0, self._title_depth - 1)
            return
        if lower in {"pre", "code"}:
            self._code_depth = max(0, self._code_depth - 1)
            if self._code_depth == 0 and self._current_code:
                code = "".join(self._current_code).strip()
                if code:
                    self._code_chunks.append(code)
                self._current_code = []

    def handle_data(self, data: str):
        if not data or self._skip_depth > 0:
            return
        if self._title_depth > 0:
            self._title_chunks.append(data)
            return
        if self._code_depth > 0:
            self._current_code.append(data)
            return
        stripped = re.sub(r"\s+", " ", data).strip()
        if stripped:
            self._text_chunks.append(stripped)

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._title_chunks)).strip()

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "\n".join(self._text_chunks)).strip()

    @property
    def code_blocks(self) -> List[str]:
        return [block for block in self._code_chunks if block.strip()]


class SourceFetchPlugin(Plugin):
    name = "source_fetch"
    display_name = "Source Fetch"
    description = "Search and fetch readable source code or docs. Supports GitHub/doc search, URL batches, and optional Scrapling/Crawl4AI extraction."
    icon = "fa-code-branch"
    security_level = SecurityLevel.L1

    _RAW_TEXT_SUFFIXES = {
        ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".java", ".js",
        ".json", ".jsx", ".mjs", ".md", ".php", ".py", ".rb", ".rs", ".sh", ".sql",
        ".svg", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
    }

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "HTTP(S) URL to fetch"},
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional batch of HTTP(S) URLs to fetch concurrently",
                },
                "query": {
                    "type": "string",
                    "description": "Optional web/source search query. Prefer GitHub/docs-oriented queries.",
                },
                "domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domain allow-list for query search, such as ['github.com', 'threejs.org']",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum search results to return when using query mode",
                    "default": 5,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return in the extracted content",
                    "default": 8000,
                },
                "prefer_code": {
                    "type": "boolean",
                    "description": "Prefer raw source / code blocks when possible",
                    "default": True,
                },
                "include_links": {
                    "type": "boolean",
                    "description": "Include a small set of links discovered on the page",
                    "default": False,
                },
                "follow_depth": {
                    "type": "integer",
                    "description": "v6.5: if >=1, after fetching the primary URL, follow up to 2-3 of the most relevant links discovered on it and return their extracts too.",
                    "default": 0,
                },
                "consumer_hint": {
                    "type": "string",
                    "description": "v6.5: downstream consumer this fetch is for (e.g. 'builder_1', 'analyst', 'polisher'). Echoed back in the result with recommended_consumers for routing.",
                },
            },
            "required": [],
        }

    # ─────────────────────────────────────────────────────
    # v6.5 Phase 2 (#12A): GitHub blob -> raw URL rewrite
    # ─────────────────────────────────────────────────────
    @staticmethod
    def _github_to_raw(url: str) -> str:
        """Rewrite https://github.com/{owner}/{repo}/blob/{branch}/{path}
        to the raw.githubusercontent.com equivalent. Pass-through otherwise."""
        m = re.match(
            r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
            str(url or "").strip(),
        )
        if m:
            return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}"
        return url

    # ─────────────────────────────────────────────────────
    # v6.5 Phase 2 (#12B): Fenced code-block extractor
    # ─────────────────────────────────────────────────────
    _FENCED_CODE_RE = re.compile(
        r"```(?P<lang>[a-zA-Z0-9_+\-]*)\s*\n(?P<code>.*?)```",
        re.DOTALL,
    )

    def _extract_code_blocks_from_text(self, text: str, max_blocks: int = 12) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not text:
            return out
        for m in self._FENCED_CODE_RE.finditer(str(text)):
            code = (m.group("code") or "").rstrip()
            if not code.strip():
                continue
            out.append({
                "lang": (m.group("lang") or "").strip().lower(),
                "code": code[:8000],
                "line_count": code.count("\n") + 1,
            })
            if len(out) >= max_blocks:
                break
        return out

    @staticmethod
    def _classify_consumer(url: str, hint: str = "") -> List[str]:
        """v6.5 Phase 2 (#12D): produce a shortlist of downstream nodes that
        would benefit from this fetch. If the caller passed a `consumer_hint`
        we honor it first; otherwise infer from the URL host/path."""
        recommendations: List[str] = []
        if hint:
            recommendations.append(hint.strip().lower())
        lower = str(url or "").lower()
        host = str(urlparse(lower).netloc or "")
        if "github.com" in host or "raw.githubusercontent.com" in host or lower.endswith((".js", ".ts", ".py", ".css", ".html")):
            for c in ("builder_1", "builder_2", "merger"):
                if c not in recommendations:
                    recommendations.append(c)
        if any(tok in lower for tok in ("docs", "tutorial", "guide", "reference")):
            for c in ("analyst", "builder_1"):
                if c not in recommendations:
                    recommendations.append(c)
        if any(tok in lower for tok in ("dribbble", "behance", "figma", "awwwards")):
            for c in ("uidesign", "polisher"):
                if c not in recommendations:
                    recommendations.append(c)
        if not recommendations:
            recommendations.append("analyst")
        return recommendations[:4]

    def _normalized_search_domains(self, value: Any) -> List[str]:
        items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
        normalized: List[str] = []
        seen: set[str] = set()
        for raw in items:
            text = str(raw or "").strip().lower()
            if not text:
                continue
            if text.startswith("http://") or text.startswith("https://"):
                text = str(urlparse(text).netloc or "").strip().lower()
            text = text.lstrip(".")
            if text.startswith("www."):
                text = text[4:]
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text[:120])
            if len(normalized) >= 12:
                break
        return normalized

    def _preferred_domains_from_context(self, context: Optional[Dict[str, Any]]) -> List[str]:
        analyst_cfg = {}
        if isinstance(context, dict) and isinstance(context.get("analyst"), dict):
            analyst_cfg = context.get("analyst") or {}
        preferred_sites = analyst_cfg.get("preferred_sites", []) if isinstance(analyst_cfg, dict) else []
        return self._normalized_search_domains(preferred_sites)

    def _query_search_enabled(self, context: Optional[Dict[str, Any]]) -> bool:
        analyst_cfg = {}
        if isinstance(context, dict) and isinstance(context.get("analyst"), dict):
            analyst_cfg = context.get("analyst") or {}
        return bool(analyst_cfg.get("enable_query_search", True))

    def _crawl_intensity(self, context: Optional[Dict[str, Any]]) -> str:
        analyst_cfg = {}
        if isinstance(context, dict) and isinstance(context.get("analyst"), dict):
            analyst_cfg = context.get("analyst") or {}
        intensity = str(analyst_cfg.get("crawl_intensity", "medium") or "medium").strip().lower()
        if intensity not in {"off", "low", "medium", "high"}:
            intensity = "medium"
        return intensity

    def _default_top_k(self, context: Optional[Dict[str, Any]]) -> int:
        return {
            "off": 2,
            "low": 3,
            "medium": 5,
            "high": 8,
        }.get(self._crawl_intensity(context), 5)

    def _max_batch_urls(self, context: Optional[Dict[str, Any]]) -> int:
        return {
            "off": 2,
            "low": 3,
            "medium": 5,
            "high": 8,
        }.get(self._crawl_intensity(context), 5)

    def _scrapling_enabled(self, context: Optional[Dict[str, Any]]) -> bool:
        analyst_cfg = {}
        if isinstance(context, dict) and isinstance(context.get("analyst"), dict):
            analyst_cfg = context.get("analyst") or {}
        return bool(analyst_cfg.get("use_scrapling_when_available", True))

    def _normalize_source_url(self, url: str, prefer_code: bool) -> str:
        parsed = urlparse(url)
        host = str(parsed.netloc or "").strip().lower()
        if host != "github.com" or not prefer_code:
            return url
        parts = [part for part in str(parsed.path or "").split("/") if part]
        if len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _blob, branch = parts[:4]
            remainder = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{remainder}"
        return url

    def _looks_like_raw_text(self, url: str, content_type: str = "") -> bool:
        parsed = urlparse(url)
        host = str(parsed.netloc or "").strip().lower()
        path = str(parsed.path or "").strip().lower()
        if host in {"raw.githubusercontent.com", "gist.githubusercontent.com"}:
            return True
        if content_type and any(token in content_type.lower() for token in ("text/plain", "application/json", "application/javascript")):
            return True
        return any(path.endswith(suffix) for suffix in self._RAW_TEXT_SUFFIXES)

    def _domain_matches(self, url: str, domains: List[str]) -> bool:
        if not domains:
            return True
        host = str(urlparse(url).netloc or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

    def _decode_duckduckgo_href(self, href: str) -> str:
        text = str(href or "").strip()
        if not text:
            return ""
        if text.startswith("//"):
            text = "https:" + text
        parsed = urlparse(text)
        if "duckduckgo.com" not in str(parsed.netloc or "").lower():
            return text
        query = parse_qs(parsed.query)
        redirected = query.get("uddg", [])
        if redirected:
            return str(redirected[0] or "").strip()
        return text

    def _strip_html_text(self, value: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", str(value or ""))
        return re.sub(r"\s+", " ", html.unescape(cleaned)).strip()

    async def _search_with_duckduckgo(
        self,
        query: str,
        domains: List[str],
        top_k: int,
    ) -> List[Dict[str, str]]:
        def _read() -> str:
            request = Request(
                "https://html.duckduckgo.com/html/",
                data=urlencode({"q": query}).encode("utf-8"),
                headers={
                    "User-Agent": "EvermindSourceFetch/1.0 (+https://evermind.local)",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urlopen(request, timeout=20) as response:
                payload = response.read(1_000_000)
                content_type = str(response.headers.get("Content-Type") or "").strip()
            return self._decode_body(payload, content_type)

        html_text = await asyncio.to_thread(_read)
        results: List[Dict[str, str]] = []
        seen: set[str] = set()
        anchor_re = re.compile(
            r"<a[^>]+class=[\"'][^\"']*result__a[^\"']*[\"'][^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in anchor_re.finditer(html_text):
            href = self._decode_duckduckgo_href(match.group(1))
            title = self._strip_html_text(match.group(2))
            if not href or not title or href in seen or not self._domain_matches(href, domains):
                continue
            seen.add(href)
            tail = html_text[match.end(): match.end() + 1200]
            snippet_match = re.search(
                r"result__snippet[^>]*>(.*?)</(?:a|div|span)>",
                tail,
                re.IGNORECASE | re.DOTALL,
            )
            snippet = self._strip_html_text(snippet_match.group(1) if snippet_match else "")
            results.append({
                "title": title[:180],
                "url": href[:500],
                "snippet": snippet[:320],
            })
            if len(results) >= top_k:
                break
        return results

    def _decode_body(self, payload: bytes, content_type: str) -> str:
        charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "", re.IGNORECASE)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            return payload.decode(charset, errors="ignore")
        except Exception:
            return payload.decode("utf-8", errors="ignore")

    def _truncate(self, text: str, max_chars: int) -> str:
        cleaned = str(text or "").strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max(0, max_chars - 3)].rstrip() + "..."

    def _extract_html_payload(
        self,
        html_text: str,
        *,
        url: str,
        max_chars: int,
        include_links: bool,
        engine: str,
    ) -> Dict[str, Any]:
        extractor = _SourceFetchHTMLExtractor()
        extractor.feed(html_text)
        title = extractor.title
        content = extractor.text or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text)).strip()
        code_blocks = [
            self._truncate(html.unescape(block), min(max_chars // 2, 2400))
            for block in extractor.code_blocks[:3]
        ]
        data: Dict[str, Any] = {
            "url": url,
            "engine": engine,
            "source_kind": "html",
            "title": title,
            "content": self._truncate(content, max_chars),
            "code_blocks": code_blocks,
        }
        if include_links:
            data["links"] = extractor.links[:12]
        return data

    def _extract_text_payload(
        self,
        text: str,
        *,
        url: str,
        max_chars: int,
        engine: str,
        source_kind: str,
    ) -> Dict[str, Any]:
        normalized = str(text or "").replace("\r\n", "\n").strip()
        code_blocks = [self._truncate(normalized, min(max_chars, 2400))]
        return {
            "url": url,
            "engine": engine,
            "source_kind": source_kind,
            "title": Path(urlparse(url).path).name or url,
            "content": self._truncate(normalized, max_chars),
            "code_blocks": code_blocks,
        }

    async def _fetch_with_scrapling(
        self,
        url: str,
        *,
        max_chars: int,
        include_links: bool,
    ) -> Optional[Dict[str, Any]]:
        try:
            from scrapling.fetchers import Fetcher  # type: ignore
        except Exception:
            return None

        def _read() -> Any:
            try:
                return Fetcher.get(url, stealthy_headers=True)  # type: ignore[attr-defined]
            except TypeError:
                return Fetcher.get(url)  # type: ignore[attr-defined]

        try:
            response = await asyncio.to_thread(_read)
        except Exception as exc:
            logger.warning("source_fetch scrapling failed for %s: %s", url, exc)
            return None

        html_text = ""
        for attr in ("html_content", "content", "body", "text", "raw_html"):
            value = getattr(response, attr, "")
            if isinstance(value, str) and value.strip():
                html_text = value
                break
        if not html_text:
            html_text = str(response or "").strip()
        if not html_text:
            return None

        data = self._extract_html_payload(
            html_text,
            url=url,
            max_chars=max_chars,
            include_links=include_links,
            engine="scrapling",
        )
        return data if data.get("content") else None

    async def _fetch_with_crawl4ai(self, url: str, max_chars: int, include_links: bool) -> Optional[Dict[str, Any]]:
        try:
            from crawl4ai import AsyncWebCrawler  # type: ignore
        except Exception:
            return None

        try:
            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(url=url)
        except Exception as exc:
            logger.warning("source_fetch crawl4ai failed for %s: %s", url, exc)
            return None

        if result is None or getattr(result, "success", True) is False:
            return None

        markdown = ""
        raw_markdown = getattr(result, "markdown", "")
        if isinstance(raw_markdown, str):
            markdown = raw_markdown
        elif raw_markdown is not None:
            for attr in ("raw_markdown", "markdown", "fit_markdown"):
                value = getattr(raw_markdown, attr, "")
                if isinstance(value, str) and value.strip():
                    markdown = value
                    break
        if not markdown:
            return None

        title = str(getattr(result, "title", "") or "").strip()
        links: List[str] = []
        if include_links:
            extracted_links = getattr(result, "links", None)
            if isinstance(extracted_links, dict):
                for bucket in extracted_links.values():
                    if isinstance(bucket, list):
                        for item in bucket:
                            href = ""
                            if isinstance(item, dict):
                                href = str(item.get("href") or item.get("url") or "").strip()
                            elif isinstance(item, str):
                                href = item.strip()
                            if href and href not in links:
                                links.append(href)
                            if len(links) >= 12:
                                break
                    if len(links) >= 12:
                        break

        return {
            "url": url,
            "engine": "crawl4ai",
            "source_kind": "html",
            "title": title or url,
            "content": self._truncate(markdown, max_chars),
            "code_blocks": [],
            "links": links[:12] if include_links else [],
        }

    async def _fetch_with_urllib(
        self,
        url: str,
        *,
        max_chars: int,
        include_links: bool,
        prefer_code: bool,
    ) -> Dict[str, Any]:
        def _read() -> Tuple[bytes, str]:
            request = Request(
                url,
                headers={
                    "User-Agent": "EvermindSourceFetch/1.0 (+https://evermind.local)",
                    "Accept": "text/html, text/plain, application/json, application/javascript;q=0.9, */*;q=0.8",
                },
            )
            with urlopen(request, timeout=20) as response:
                content_type = str(response.headers.get("Content-Type") or "").strip()
                payload = response.read(2_000_000)
            return payload, content_type

        payload, content_type = await asyncio.to_thread(_read)
        text = self._decode_body(payload, content_type)
        if self._looks_like_raw_text(url, content_type) or (prefer_code and not text.lstrip().startswith("<")):
            return self._extract_text_payload(
                text,
                url=url,
                max_chars=max_chars,
                engine="urllib",
                source_kind="raw_text",
            )
        return self._extract_html_payload(
            text,
            url=url,
            max_chars=max_chars,
            include_links=include_links,
            engine="urllib",
        )

    async def _fetch_single_source(
        self,
        url: str,
        *,
        max_chars: int,
        include_links: bool,
        prefer_code: bool,
        context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        normalized_url = self._normalize_source_url(url, prefer_code)
        result = None
        if not self._looks_like_raw_text(normalized_url):
            if self._scrapling_enabled(context):
                result = await self._fetch_with_scrapling(
                    normalized_url,
                    max_chars=max_chars,
                    include_links=include_links,
                )
            if result is None:
                result = await self._fetch_with_crawl4ai(normalized_url, max_chars, include_links)
        if result is None:
            result = await self._fetch_with_urllib(
                normalized_url,
                max_chars=max_chars,
                include_links=include_links,
                prefer_code=prefer_code,
            )
        if normalized_url != url:
            result["requested_url"] = url
            result["resolved_url"] = normalized_url
        return result

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        url = str(params.get("url") or "").strip()
        urls = [
            str(item or "").strip()
            for item in (params.get("urls") if isinstance(params.get("urls"), list) else [])
            if str(item or "").strip()
        ]
        query = str(params.get("query") or "").strip()
        if not url and not urls and not query:
            return PluginResult(success=False, error="Missing required parameter: url, urls, or query")

        # v6.5 Phase 2 (#12A): rewrite github.com/.../blob/... to raw.githubusercontent.com
        # before any expensive fetch, so we avoid the ~100KB HTML wrapper and
        # get direct source text.
        if url:
            url = self._github_to_raw(url)
        if urls:
            urls = [self._github_to_raw(u) for u in urls]

        prefer_code = bool(params.get("prefer_code", True))
        include_links_param = bool(params.get("include_links", False))
        try:
            max_chars = int(params.get("max_chars", 8000) or 8000)
        except Exception:
            max_chars = 8000
        max_chars = max(800, min(max_chars, 24000))
        try:
            top_k = int(params.get("top_k", self._default_top_k(context)) or self._default_top_k(context))
        except Exception:
            top_k = self._default_top_k(context)
        top_k = max(1, min(top_k, 12))

        # v6.5 Phase 2 (#12C,#12D): follow_depth + consumer classification
        try:
            follow_depth = int(params.get("follow_depth", 0) or 0)
        except Exception:
            follow_depth = 0
        follow_depth = max(0, min(follow_depth, 2))
        consumer_hint = str(params.get("consumer_hint") or "").strip().lower()
        # Forcing include_links=True when follow_depth requested so we have
        # candidate anchors to crawl without a second round-trip.
        include_links = include_links_param or follow_depth >= 1

        domains = self._normalized_search_domains(params.get("domains", []))
        if not domains:
            domains = self._preferred_domains_from_context(context)

        if query:
            if not self._query_search_enabled(context):
                return PluginResult(success=False, error="source_fetch query search is disabled by analyst settings")
            try:
                results = await self._search_with_duckduckgo(query, domains, top_k)
                return PluginResult(success=True, data={
                    "engine": "duckduckgo_html",
                    "mode": "search",
                    "query": query,
                    "domains": domains,
                    "results": results,
                    "recommended_consumers": self._classify_consumer(query, consumer_hint),
                })
            except (HTTPError, URLError) as exc:
                return PluginResult(success=False, error=f"Failed to search sources for '{query}': {exc}")
            except Exception as exc:
                return PluginResult(success=False, error=f"source_fetch search failed for '{query}': {exc}")

        target_urls = [url] if url else urls
        for candidate in target_urls:
            parsed = urlparse(candidate)
            if str(parsed.scheme or "").strip().lower() not in {"http", "https"}:
                return PluginResult(success=False, error="source_fetch only supports http/https URLs")

        try:
            if len(target_urls) > 1:
                tasks = [
                    self._fetch_single_source(
                        candidate,
                        max_chars=max_chars,
                        include_links=include_links,
                        prefer_code=prefer_code,
                        context=context,
                    )
                    for candidate in target_urls[: self._max_batch_urls(context)]
                ]
                results = await asyncio.gather(*tasks)
                # v6.5: enrich each item with code_blocks (if HTML/md) + consumer
                for item in results:
                    self._enrich_fetch_item(item, consumer_hint)
                return PluginResult(success=True, data={
                    "engine": "batch",
                    "mode": "batch_fetch",
                    "count": len(results),
                    "items": results,
                    "recommended_consumers": self._classify_consumer(
                        target_urls[0] if target_urls else "", consumer_hint,
                    ),
                })

            primary = await self._fetch_single_source(
                target_urls[0],
                max_chars=max_chars,
                include_links=include_links,
                prefer_code=prefer_code,
                context=context,
            )
            self._enrich_fetch_item(primary, consumer_hint)

            # v6.5 Phase 2 (#12C): follow_depth — chase 2-3 most relevant links
            followed: List[Dict[str, Any]] = []
            if follow_depth >= 1:
                candidate_links = self._rank_follow_links(primary, target_urls[0])
                follow_cap = 3 if follow_depth >= 2 else 2
                for link in candidate_links[:follow_cap]:
                    try:
                        sub = await self._fetch_single_source(
                            link,
                            max_chars=min(max_chars, 6000),
                            include_links=False,
                            prefer_code=prefer_code,
                            context=context,
                        )
                        self._enrich_fetch_item(sub, consumer_hint)
                        sub["followed_from"] = target_urls[0]
                        followed.append(sub)
                    except Exception as _follow_err:
                        logger.debug("source_fetch follow failed for %s: %s", link, _follow_err)
                if followed:
                    primary["followed"] = followed
                    primary["follow_depth"] = follow_depth
            return PluginResult(success=True, data=primary)
        except (HTTPError, URLError) as exc:
            return PluginResult(success=False, error=f"Failed to fetch {target_urls[0]}: {exc}")
        except Exception as exc:
            if len(target_urls) > 1:
                return PluginResult(success=False, error=f"source_fetch batch failed: {exc}")
            return PluginResult(success=False, error=f"source_fetch failed for {target_urls[0]}: {exc}")

    # ─────────────────────────────────────────────────────
    # v6.5 Phase 2 helpers (#12B, #12C, #12D wiring)
    # ─────────────────────────────────────────────────────
    def _enrich_fetch_item(self, item: Dict[str, Any], consumer_hint: str) -> None:
        """In-place: add code_blocks (from content if markdown/html) + consumer hint."""
        if not isinstance(item, dict):
            return
        # If scrapling/urllib already populated code_blocks, keep them; else
        # re-extract from the content field (which may be raw markdown).
        existing = item.get("code_blocks")
        if not existing and isinstance(item.get("content"), str):
            try:
                extracted = self._extract_code_blocks_from_text(item["content"], max_blocks=6)
                if extracted:
                    item["code_blocks"] = extracted
            except Exception:
                pass
        item["recommended_consumers"] = self._classify_consumer(
            str(item.get("url") or ""), consumer_hint,
        )

    _FOLLOW_SCORING_TOKENS = (
        "example", "readme", "docs", "getting-started", "tutorial",
        "src", "index", "api", "reference", ".md", ".js", ".ts", ".py",
    )

    def _rank_follow_links(self, primary: Dict[str, Any], base_url: str) -> List[str]:
        links = primary.get("links") if isinstance(primary, dict) else None
        if not isinstance(links, list):
            return []
        scored: List[Tuple[int, str]] = []
        seen: set[str] = set()
        base_host = str(urlparse(str(base_url or "")).netloc or "").lower()
        for raw in links:
            href = ""
            if isinstance(raw, str):
                href = raw.strip()
            elif isinstance(raw, dict):
                href = str(raw.get("href") or raw.get("url") or "").strip()
            if not href or href.startswith(("#", "mailto:", "javascript:")):
                continue
            # Only follow same-host links by default to stay on-topic
            parsed = urlparse(href)
            if not parsed.netloc and base_url:
                # Relative link — resolve against base
                from urllib.parse import urljoin
                href = urljoin(base_url, href)
                parsed = urlparse(href)
            host = str(parsed.netloc or "").lower()
            if host and host != base_host:
                # Allow raw-github ↔ github cross-follow
                if not (("github.com" in host and "github" in base_host) or
                        ("githubusercontent.com" in host and "github" in base_host)):
                    continue
            if href in seen:
                continue
            seen.add(href)
            lower = href.lower()
            score = sum(2 for tok in self._FOLLOW_SCORING_TOKENS if tok in lower)
            if any(lower.endswith(ext) for ext in (".md", ".js", ".ts", ".py", ".html")):
                score += 3
            scored.append((score, href))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [href for _, href in scored[:6]]


# ─────────────────────────────────────────────
# 4. File Operations Plugin
# ─────────────────────────────────────────────
class _PatchApplyError(Exception):
    """Raised when a unified diff cannot be cleanly applied."""


def _apply_unified_diff(original: str, patch_text: str) -> str:
    """Apply a unified diff to `original` and return the patched text.

    v6.1.6 (Opus R2 rewrite) — correct multi-hunk handling. Processes the
    file line-by-line, advancing a single cursor through `remaining`. Each
    hunk is anchored by searching for its full before-block (context + '-'
    lines) starting at the current cursor; everything between the previous
    cursor and the anchor is preserved verbatim. Supports empty-before
    (new-file creation via /dev/null).

    Raises _PatchApplyError when any hunk cannot be anchored.
    """
    patch_lines = patch_text.splitlines(keepends=True)
    # Detect new-file creation: --- /dev/null → +++ path
    header_dev_null = any(
        ln.startswith("---") and "/dev/null" in ln for ln in patch_lines[:5]
    )
    is_new_file = header_dev_null and not original

    # Skip through header until first @@
    pi = 0
    while pi < len(patch_lines) and not patch_lines[pi].startswith("@@"):
        pi += 1
    if pi >= len(patch_lines):
        raise _PatchApplyError("no @@ hunk header found")

    # If new-file creation, just emit all '+' lines.
    if is_new_file:
        collected: List[str] = []
        while pi < len(patch_lines):
            ln = patch_lines[pi]
            if ln.startswith("+") and not ln.startswith("+++"):
                collected.append(ln[1:])
            pi += 1
        return "".join(collected)

    # Multi-hunk apply via string search with a single forward cursor
    result: List[str] = []
    cursor = 0  # index into `original`
    while pi < len(patch_lines):
        if not patch_lines[pi].startswith("@@"):
            pi += 1
            continue
        pi += 1  # past @@ header
        before_parts: List[str] = []
        after_parts: List[str] = []
        while pi < len(patch_lines) and not patch_lines[pi].startswith("@@"):
            ln = patch_lines[pi]
            if ln.startswith("---") or ln.startswith("+++"):
                pi += 1
                continue
            if ln.startswith("+") and not ln.startswith("+++"):
                after_parts.append(ln[1:])
            elif ln.startswith("-") and not ln.startswith("---"):
                before_parts.append(ln[1:])
            elif ln.startswith(" "):
                before_parts.append(ln[1:])
                after_parts.append(ln[1:])
            elif ln.strip() == "":
                # Blank line in patch = empty context line
                before_parts.append("\n")
                after_parts.append("\n")
            pi += 1
        before = "".join(before_parts)
        after = "".join(after_parts)
        if not before and not after:
            continue

        # Anchor `before` in original[cursor:]. Fuzzy: try exact, then trim
        # trailing newline, then reduce context by 1 from each end iteratively.
        def _anchor(haystack: str, needle: str) -> int:
            if not needle:
                return 0
            idx = haystack.find(needle)
            if idx >= 0:
                return idx
            # fuzzy: try stripping a trailing newline
            if needle.endswith("\n"):
                idx = haystack.find(needle.rstrip("\n"))
                if idx >= 0:
                    return idx
            return -1

        idx = _anchor(original[cursor:], before) if before else 0
        if idx < 0:
            raise _PatchApplyError(f"hunk not found at or after offset {cursor}")
        # Emit text up to anchor + replacement
        anchor_abs = cursor + idx
        result.append(original[cursor:anchor_abs])
        result.append(after)
        cursor = anchor_abs + len(before)

    # Tail
    result.append(original[cursor:])
    return "".join(result)


class FileOpsPlugin(Plugin):
    name = "file_ops"
    display_name = "File Ops"
    description = "Read, write, list, and manage local files"
    icon = "fa-file"
    security_level = SecurityLevel.L2
    _DELIVERABLE_REPLAY_MARKERS = (
        "<omitted large file content during replay>",
        "OLDER_CONTEXT_OMITTED",
        "[TRUNCATED]",
    )
    _UNSAFE_PATH_CHAR_RE = re.compile(r"[\x00\u200b\u200c\u200d\u2060\ufeff]")

    def _resolve_candidate_path(self, path: str) -> Path:
        candidate = Path(str(path or "")).expanduser()
        if candidate.exists():
            return candidate.resolve()
        try:
            return candidate.parent.resolve() / candidate.name
        except Exception:
            return candidate.absolute()

    def _is_allowed_path(self, path: str, allowed_dirs) -> bool:
        if not path:
            return False
        resolved = self._resolve_candidate_path(path)

        roots = []
        for allowed in allowed_dirs or []:
            try:
                roots.append(Path(allowed).expanduser().resolve())
            except Exception:
                continue

        return any(resolved == root or root in resolved.parents or resolved.parent == root or root in resolved.parent.parents for root in roots)

    def _guard_unsafe_path_text(self, path: str, action: str) -> Optional[PluginResult]:
        raw = str(path or "")
        if not raw:
            return None
        if self._UNSAFE_PATH_CHAR_RE.search(raw):
            logger.warning("Rejected file_ops %s due to unsafe path characters: %r", action, raw)
            return PluginResult(
                success=False,
                error=(
                    f"Unsafe path rejected for file_ops action '{action}': filenames may not contain null bytes "
                    "or invisible zero-width Unicode characters."
                ),
            )
        return None

    def _guard_output_dir_escape(self, path: str, action: str, context: Dict[str, Any]) -> Optional[PluginResult]:
        if action not in {"write", "edit", "delete", "patch"}:
            return None
        output_root_raw = str(
            (context or {}).get("file_ops_output_dir")
            or (context or {}).get("output_dir")
            or ""
        ).strip()
        if not output_root_raw:
            return None
        try:
            output_root = Path(output_root_raw).expanduser().resolve()
            candidate = self._resolve_candidate_path(path)
        except Exception:
            return None
        if candidate == output_root or output_root in candidate.parents:
            return None
        # V4.6 SPEED: Auto-correct path instead of rejecting.
        # When model writes to parent dir (e.g. output/index.html instead of
        # output/task_6/index.html), redirect into output_root automatically.
        try:
            parent_of_root = output_root.parent
            if parent_of_root in candidate.parents or candidate.parent == parent_of_root:
                rel_from_parent = candidate.relative_to(parent_of_root)
                corrected = output_root / rel_from_parent
                logger.info(
                    "Auto-corrected file_ops %s path (parent_of_root): %s -> %s",
                    action, candidate, corrected,
                )
                context["_auto_corrected_path"] = str(corrected)
                return None
        except (ValueError, RuntimeError):
            pass
        # v6.4.28 (maintainer 2026-04-22) — AGGRESSIVE basename re-map.
        # Observed bug: kimi patcher resolves "index.html" against Python's
        # CWD (Evermind.app/Contents/Resources/backend) producing an absolute
        # path INSIDE the app bundle. The sandbox correctly rejected the
        # write but the patcher didn't know why, spun through 20+ empty
        # tool_calls, and ultimately failed the whole round.
        # Fix: if the candidate's BASENAME already exists under output_root
        # (or is a well-known output artifact name), silently re-map to the
        # output_root version. This is safe because:
        #   (a) we only accept the candidate's basename, not its escape path
        #   (b) we require the target file to already exist in output_root,
        #       meaning the user/pipeline intended to edit THAT file
        #   (c) we whitelist common filenames as a fallback when nothing
        #       exists yet
        try:
            basename = candidate.name
            existing_match = output_root / basename
            if existing_match.exists() and existing_match.is_file():
                logger.info(
                    "Auto-corrected file_ops %s (basename match): %s -> %s "
                    "(agent resolved against wrong CWD; redirecting to output_root)",
                    action, candidate, existing_match,
                )
                context["_auto_corrected_path"] = str(existing_match)
                return None
            # Conservative whitelist: common output artifact filenames.
            _WELL_KNOWN_BASENAMES = {
                "index.html", "main.html", "game.html", "app.html",
                "styles.css", "style.css", "main.css", "app.css",
                "app.js", "main.js", "game.js", "script.js",
                "manifest.json", "config.json", "sprite_config.json",
                "sprites.js", "loader.js", "visual_config.json",
            }
            if basename.lower() in _WELL_KNOWN_BASENAMES:
                corrected = output_root / basename
                logger.info(
                    "Auto-corrected file_ops %s (whitelisted basename): %s -> %s",
                    action, candidate, corrected,
                )
                context["_auto_corrected_path"] = str(corrected)
                return None
            # Path inside Evermind install bundle or source repo: strong
            # signal of CWD-resolution bug — redirect to output_root by name.
            _bad_prefixes = (
                "/Applications/Evermind",
                "/Users/", "/Volumes/",  # any user dir is suspicious too
            )
            candidate_str = str(candidate)
            if "Evermind.app" in candidate_str or "/Resources/backend" in candidate_str:
                corrected = output_root / basename
                logger.info(
                    "Auto-corrected file_ops %s (inside Evermind bundle — CWD bug): %s -> %s",
                    action, candidate, corrected,
                )
                context["_auto_corrected_path"] = str(corrected)
                return None
        except Exception as _remap_err:
            logger.debug("Path auto-correct attempt failed: %s", _remap_err)
        logger.warning(
            "Rejected file_ops %s escaping output dir: candidate=%s output_root=%s",
            action,
            candidate,
            output_root,
        )
        return PluginResult(
            success=False,
            error=(
                f"Path '{candidate}' is outside the runtime output directory. "
                f"For action '{action}', please pass path as an ABSOLUTE path under {output_root}, "
                f"e.g. '{output_root}/index.html'. Relative paths resolve against the backend CWD "
                f"which is inside the Evermind install bundle — NOT your deliverable target. "
                f"Retry with the correct absolute path."
            ),
        )

    def _relative_to_output_root(self, path: Path, context: Dict[str, Any]) -> Optional[Path]:
        output_root_raw = str(
            (context or {}).get("file_ops_output_dir")
            or (context or {}).get("output_dir")
            or ""
        ).strip()
        if not output_root_raw:
            return None
        try:
            output_root = Path(output_root_raw).expanduser().resolve()
            return path.relative_to(output_root)
        except Exception:
            return None

    def _enforce_builder_html_targets(self, action: str, path: str, context: Dict[str, Any]) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None
        node_type = str((context or {}).get("file_ops_node_type", "") or "").strip().lower()
        if node_type != "builder":
            return None

        candidate = self._resolve_candidate_path(path)
        if candidate.suffix.lower() not in (".html", ".htm"):
            return None

        allowed_targets = [
            Path(str(item).strip()).name
            for item in ((context or {}).get("file_ops_allowed_html_targets") or [])
            if str(item).strip()
        ]
        strict_target_boundaries = bool((context or {}).get("file_ops_enforce_html_targets"))

        output_root_raw = str(
            (context or {}).get("file_ops_output_dir")
            or (context or {}).get("output_dir")
            or ""
        ).strip()
        relative = None
        if output_root_raw:
            try:
                output_root = Path(output_root_raw).expanduser().resolve()
                relative = candidate.relative_to(output_root)
            except Exception:
                if strict_target_boundaries:
                    return PluginResult(
                        success=False,
                        error=(
                            "Builder HTML writes must stay inside the runtime output directory. "
                            f"Blocked path: {candidate}"
                        ),
                    )

        basename = candidate.name
        can_write_root_index = bool((context or {}).get("file_ops_can_write_root_index"))
        writes_root_deliverable = bool(relative and len(relative.parts) == 1)

        if basename == "index.html" and writes_root_deliverable and not can_write_root_index:
            return PluginResult(
                success=False,
                error=(
                    "HTML target not assigned for builder: index.html. "
                    + (
                        f"Allowed HTML filenames: {', '.join(allowed_targets)}"
                        if allowed_targets else
                        "This secondary builder must not overwrite the root gameplay/site shell."
                    )
                ),
            )

        if not strict_target_boundaries:
            if writes_root_deliverable and not can_write_root_index:
                if basename == "index.html" or not allowed_targets or basename not in allowed_targets:
                    return PluginResult(
                        success=False,
                        error=(
                            "Secondary builder may not write deliverable root HTML directly in a single-output task. "
                            "Return the improved full HTML in the model response instead of overwriting the live root artifact."
                        ),
                    )
            return None

        if not allowed_targets:
            return None
        if not writes_root_deliverable:
            return PluginResult(
                success=False,
                error=(
                    "Builder HTML files must be written directly under the runtime output directory. "
                    f"Blocked path: {candidate}"
                ),
            )
        if basename not in allowed_targets:
            return PluginResult(
                success=False,
                error=(
                    f"HTML target not assigned for builder: {basename}. "
                    f"Allowed HTML filenames: {', '.join(allowed_targets)}"
                ),
            )
        return None

    def _enforce_deliverable_html_write_ownership(
        self,
        action: str,
        path: str,
        context: Dict[str, Any],
    ) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None

        node_type = str((context or {}).get("file_ops_node_type", "") or "").strip().lower()
        if node_type in {"builder", "polisher", "debugger", "merger"}:
            return None

        candidate = self._resolve_candidate_path(path)
        if candidate.suffix.lower() not in (".html", ".htm"):
            return None

        output_root_raw = str(
            (context or {}).get("file_ops_output_dir")
            or (context or {}).get("output_dir")
            or ""
        ).strip()
        if not output_root_raw:
            return None

        try:
            output_root = Path(output_root_raw).expanduser().resolve()
            relative = candidate.relative_to(output_root)
        except Exception:
            return None

        # Protect deliverable pages directly under the runtime output directory.
        if len(relative.parts) != 1:
            return None

        return PluginResult(
            success=False,
            error=(
                f"{node_type or 'node'} is not allowed to write deliverable HTML in the runtime output directory: "
                f"{candidate}"
            ),
        )

    def _enforce_builder_shared_root_asset_ownership(
        self,
        action: str,
        path: str,
        context: Dict[str, Any],
    ) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None

        node_type = str((context or {}).get("file_ops_node_type", "") or "").strip().lower()
        if node_type != "builder":
            return None

        candidate = self._resolve_candidate_path(path)
        if candidate.suffix.lower() not in (".css", ".js"):
            return None

        if candidate.name.lower() not in _SHARED_ROOT_ASSET_NAMES:
            return None

        relative = self._relative_to_output_root(candidate, context or {})
        if relative is None or len(relative.parts) != 1:
            return None

        if bool((context or {}).get("file_ops_can_write_root_index")):
            return None

        return PluginResult(
            success=False,
            error=(
                f"Secondary builder may not overwrite shared root asset {candidate.name}. "
                "Root shared assets belong to the primary builder-owned live artifact; "
                "keep changes inside your assigned HTML pages or route-local assets instead."
            ),
        )

    def _enforce_active_write_token(self, action: str, context: Dict[str, Any]) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None

        node_type = str((context or {}).get("file_ops_node_type", "") or "").strip().lower()
        if node_type != "builder":
            return None

        node_execution_id = str((context or {}).get("node_execution_id", "") or "").strip()
        token = str((context or {}).get("file_ops_write_token", "") or "").strip()
        if not node_execution_id or not token:
            return None

        active_entry = _ACTIVE_FILE_OPS_WRITE_TOKENS.get(node_execution_id)
        active = active_entry[0] if active_entry else None
        if active == token:
            return None
        if not active:
            return PluginResult(
                success=False,
                error=(
                    "Inactive builder file_ops write rejected because this builder attempt is no longer active. "
                    "Re-read the latest artifact and write from the current live attempt only."
                ),
            )

        return PluginResult(
            success=False,
            error=(
                "Stale builder file_ops write rejected because a newer builder attempt is already active. "
                "Re-read the latest artifact and write from the current attempt only."
            ),
        )

    def _validate_deliverable_html_write(
        self,
        action: str,
        path: str,
        content: Any,
        context: Dict[str, Any],
    ) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None

        node_type = str((context or {}).get("file_ops_node_type", "") or "").strip().lower()
        if node_type not in {"builder", "polisher", "debugger"}:
            return None

        candidate = self._resolve_candidate_path(path)
        if candidate.suffix.lower() not in (".html", ".htm"):
            return None

        integrity = inspect_html_integrity(str(content or ""))
        if not integrity.get("ok", True):
            issues = "; ".join(str(item) for item in (integrity.get("errors") or [])[:4])
            return PluginResult(
                success=False,
                error=(
                    f"{node_type.title()} HTML write rejected for {candidate.name}: {issues}. "
                    "Write a complete standalone HTML document instead of a truncated or scaffold placeholder."
                ),
            )

        # v6.1.7 (maintainer 2026-04-19): 3D vs 2D contract validation. If the
        # task brief explicitly demanded 3D / Three.js / WebGL and the
        # writer produced a 2D Canvas game, reject so the model retries.
        goal_hint = str((context or {}).get("goal") or (context or {}).get("task_description") or "").lower()
        html_text = str(content or "")
        demands_3d = any(
            kw in goal_hint
            for kw in ("3d ", "three.js", "threejs", "fps ", " tps ", "webgl", "第三人称", "第一人称", "3d shooter")
        )
        if demands_3d and candidate.name.lower() == "index.html":
            has_2d_ctx = "getcontext('2d')" in html_text.lower() or 'getcontext("2d")' in html_text.lower()
            has_3d_scene = (
                "new three.scene" in html_text.lower()
                or "webglrenderer" in html_text.lower()
                or "getcontext('webgl" in html_text.lower()
                or 'getcontext("webgl' in html_text.lower()
            )
            if has_2d_ctx and not has_3d_scene:
                return PluginResult(
                    success=False,
                    error=(
                        f"{node_type.title()} contract violation for {candidate.name}: "
                        "task brief requires 3D/Three.js but produced a 2D Canvas game "
                        "(getContext('2d') without THREE.Scene/WebGLRenderer). "
                        "Re-plan with Three.js: new THREE.Scene() + PerspectiveCamera + "
                        "WebGLRenderer are REQUIRED for this brief."
                    ),
                )
        return None

    def _guard_builder_html_regression(
        self,
        action: str,
        path: str,
        content: Any,
        context: Dict[str, Any],
    ) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None

        node_type = str((context or {}).get("file_ops_node_type", "") or "").strip().lower()
        if node_type not in {"builder", "polisher"}:
            return None

        candidate = self._resolve_candidate_path(path)
        if candidate.suffix.lower() not in (".html", ".htm"):
            return None
        if not candidate.exists() or not candidate.is_file() or is_bootstrap_html_artifact(candidate):
            return None

        try:
            existing_html = candidate.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
        if not inspect_html_integrity(existing_html).get("ok", True):
            return None

        new_html = str(content or "")
        existing_report = validate_html_content(existing_html)
        new_report = validate_html_content(new_html)
        existing_score = int(existing_report.get("score", 0) or 0)
        new_score = int(new_report.get("score", 0) or 0)
        existing_bytes = len(existing_html.encode("utf-8"))
        new_bytes = len(new_html.encode("utf-8"))

        severe_score_drop = new_score + 18 < existing_score
        sharp_size_drop = existing_bytes >= 5000 and new_bytes < max(1800, int(existing_bytes * 0.4))
        lost_pass_state = bool(existing_report.get("score", 0) >= 70) and bool(new_report.get("score", 0) < 55)
        if not ((severe_score_drop and sharp_size_drop) or (lost_pass_state and new_bytes < existing_bytes)):
            return None

        return PluginResult(
            success=False,
            error=(
                f"{node_type.title()} HTML write rejected for {candidate.name}: the new page looks like a regression "
                f"versus the existing artifact ({new_bytes}B/{new_score} vs {existing_bytes}B/{existing_score}). "
                "Do not overwrite a stronger page with a thinner or lower-quality rewrite."
            ),
        )

    def _guard_deliverable_placeholder_write(
        self,
        action: str,
        path: str,
        content: Any,
        context: Dict[str, Any],
    ) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None

        node_type = str((context or {}).get("file_ops_node_type", "") or "").strip().lower()
        if node_type not in {"builder", "polisher", "debugger"}:
            return None

        candidate = self._resolve_candidate_path(path)
        if candidate.suffix.lower() not in (".html", ".htm", ".css", ".js"):
            return None

        relative = self._relative_to_output_root(candidate, context or {})
        if relative is None:
            return None

        text = str(content or "")
        hit = next((marker for marker in self._DELIVERABLE_REPLAY_MARKERS if marker in text), "")
        if not hit:
            return None

        return PluginResult(
            success=False,
            error=(
                f"{node_type.title()} write rejected for {candidate.name}: content contains replay/truncation "
                "placeholder text or a truncation marker instead of real source code. "
                "Re-read the artifact and write the full file."
            ),
        )

    def _guard_shared_asset_regression(
        self,
        action: str,
        path: str,
        content: Any,
        context: Dict[str, Any],
    ) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None

        node_type = str((context or {}).get("file_ops_node_type", "") or "").strip().lower()
        if node_type not in {"builder", "polisher", "debugger"}:
            return None

        candidate = self._resolve_candidate_path(path)
        if candidate.suffix.lower() not in (".css", ".js"):
            return None

        relative = self._relative_to_output_root(candidate, context or {})
        if relative is None or len(relative.parts) != 1:
            return None
        if not candidate.exists() or not candidate.is_file():
            return None

        try:
            existing_text = candidate.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

        existing_bytes = len(existing_text.encode("utf-8"))
        if existing_bytes < 1500:
            return None

        new_text = str(content or "")
        new_bytes = len(new_text.encode("utf-8"))
        severe_size_drop = new_bytes < max(300, int(existing_bytes * 0.15))
        if not severe_size_drop:
            return None

        return PluginResult(
            success=False,
            error=(
                f"{node_type.title()} asset write rejected for {candidate.name}: the new file looks like a severe "
                f"regression versus the existing shared asset ({new_bytes}B vs {existing_bytes}B). "
                "Do not replace a working shared stylesheet/script with a thin placeholder or collapsed rewrite."
            ),
        )

    def _default_builder_blank_path(
        self,
        *,
        action: str,
        output_root: str,
        allowed_html_targets: List[str],
        context: Dict[str, Any],
    ) -> str:
        if not output_root:
            return ""
        output_dir = Path(output_root).expanduser()
        preferred_targets: List[str] = []
        can_write_root_index = bool((context or {}).get("file_ops_can_write_root_index"))
        if can_write_root_index and "index.html" in allowed_html_targets:
            preferred_targets.append("index.html")
        preferred_targets.extend(name for name in allowed_html_targets if name not in preferred_targets)

        if action == "read":
            for name in preferred_targets:
                candidate = output_dir / name
                if candidate.exists() and candidate.is_file():
                    return str(candidate)
        if action == "write" and preferred_targets:
            for name in preferred_targets:
                candidate = output_dir / name
                if candidate.exists() and candidate.is_file():
                    return str(candidate)
            return str(output_dir / preferred_targets[0])

        fallback_candidates: List[Path] = []
        index_candidate = output_dir / "index.html"
        if index_candidate not in fallback_candidates:
            fallback_candidates.append(index_candidate)
        try:
            fallback_candidates.extend(
                candidate
                for candidate in sorted(output_dir.glob("*.htm*"))
                if candidate not in fallback_candidates
            )
        except Exception:
            pass

        if action == "read":
            for candidate in fallback_candidates:
                if candidate.exists() and candidate.is_file():
                    return str(candidate)
        if action == "write" and fallback_candidates:
            for candidate in fallback_candidates:
                if candidate.exists() and candidate.is_file():
                    return str(candidate)
            return str(fallback_candidates[0])
        return ""

    def _file_ops_attempt_key(self, context: Dict[str, Any]) -> str:
        node_execution_id = str((context or {}).get("node_execution_id", "") or "").strip()
        token = str((context or {}).get("file_ops_write_token", "") or "").strip()
        if token and node_execution_id:
            return f"{node_execution_id}:{token}"
        return node_execution_id

    def _record_builder_read(self, path: str, context: Dict[str, Any]) -> None:
        if str((context or {}).get("file_ops_node_type", "") or "").strip().lower() != "builder":
            return
        attempt_key = self._file_ops_attempt_key(context or {})
        if not attempt_key:
            return
        candidate = self._resolve_candidate_path(path)
        relative = self._relative_to_output_root(candidate, context or {})
        names = {candidate.name}
        if relative is not None:
            names.add(relative.as_posix())
            names.add(relative.name)
        bucket = _FILE_OPS_READ_HISTORY.setdefault(attempt_key, set())
        bucket.update(name for name in names if str(name or "").strip())

    def _enforce_builder_patch_read_before_write(
        self,
        action: str,
        path: str,
        context: Dict[str, Any],
    ) -> Optional[PluginResult]:
        if action not in ("write", "edit"):
            return None
        if str((context or {}).get("file_ops_node_type", "") or "").strip().lower() != "builder":
            return None
        if not bool((context or {}).get("file_ops_require_existing_artifact_read")):
            return None

        required_targets = [
            Path(str(item).strip()).name
            for item in ((context or {}).get("file_ops_required_read_targets") or [])
            if str(item).strip()
        ]
        if not required_targets:
            return None

        candidate = self._resolve_candidate_path(path)
        relative = self._relative_to_output_root(candidate, context or {})
        candidate_names = {candidate.name}
        if relative is not None:
            candidate_names.add(relative.as_posix())
            candidate_names.add(relative.name)
        if not any(name in candidate_names for name in required_targets):
            return None

        attempt_key = self._file_ops_attempt_key(context or {})
        seen_reads = _FILE_OPS_READ_HISTORY.get(attempt_key, set()) if attempt_key else set()
        if any(name in seen_reads for name in required_targets):
            return None

        return PluginResult(
            success=False,
            error=(
                "Builder patch-mode write blocked: you must file_ops read the current live artifact first. "
                f"Required read target(s): {', '.join(required_targets)}."
            ),
        )

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            action = params.get("action", "read")
            path = params.get("path", "")
            mode = str((context or {}).get("file_ops_mode", "read_write") or "read_write").strip().lower()
            if mode == "read_only" and action in {"write", "edit", "delete"}:
                node_type = str((context or {}).get("file_ops_node_type", "node") or "node")
                return PluginResult(
                    success=False,
                    error=f"file_ops is read-only for {node_type}; action '{action}' is blocked",
                )
            # v6.1.3 (maintainer 2026-04-19): patch-only mode for reviewer-rejection
            # retries. When the orchestrator sets
            # `file_ops_patch_only_mode=True`, `action=write` is REJECTED with
            # an instructional error telling the model to use `action=edit`
            # with a targeted SEARCH/REPLACE string swap on the existing
            # artifact. This prevents the regression pattern where builder
            # retries overwrite whole files and lose previously-working
            # systems (enemy_ai, progression, etc.). Based on Aider/Cline
            # SEARCH-REPLACE pattern — proven anti-regression mechanism.
            if (
                action == "write"
                and bool((context or {}).get("file_ops_patch_only_mode"))
            ):
                _existing_targets = (context or {}).get("file_ops_required_read_targets") or []
                _target_hint = ""
                if isinstance(_existing_targets, list) and _existing_targets:
                    _target_hint = (
                        f" Existing files: {', '.join(str(t) for t in _existing_targets[:4])}."
                    )
                return PluginResult(
                    success=False,
                    error=(
                        "file_ops action=write is BLOCKED on this retry (patch-only mode). "
                        "The reviewer rejected specific issues, not the whole file. "
                        "Do NOT rewrite the entire file. Use action=edit with a "
                        "targeted string replacement:\n"
                        '  { "action": "edit", "path": "index.html", '
                        '"old_string": "<exact existing code>", '
                        '"new_string": "<fixed replacement>" }\n'
                        "Preserve all other code verbatim. Each edit should touch ≤30 lines. "
                        "Use multiple small edit calls rather than one giant one."
                        + _target_hint
                    ),
                )
            unsafe_path_guard = self._guard_unsafe_path_text(str(path or ""), str(action))
            if unsafe_path_guard is not None:
                return unsafe_path_guard

            output_root = str(
                (context or {}).get("file_ops_output_dir")
                or (context or {}).get("output_dir")
                or ""
            ).strip()
            node_type = str((context or {}).get("file_ops_node_type", "node") or "node")
            allowed_html_targets = [
                Path(str(item).strip()).name
                for item in ((context or {}).get("file_ops_allowed_html_targets") or [])
                if str(item).strip()
            ]
            if not str(path or "").strip():
                if action == "list" and output_root:
                    path = output_root
                elif node_type in {"builder", "polisher", "debugger"} and action in {"read", "write"}:
                    path = self._default_builder_blank_path(
                        action=action,
                        output_root=output_root,
                        allowed_html_targets=allowed_html_targets,
                        context=context or {},
                    )
                else:
                    hint_parts = []
                    if output_root:
                        hint_parts.append(f"Use a path under {output_root}.")
                    if allowed_html_targets:
                        hint_parts.append(
                            "Allowed HTML filenames: " + ", ".join(allowed_html_targets) + "."
                        )
                    elif output_root:
                        # Dynamically discover existing HTML files for guidance
                        try:
                            existing = sorted(
                                f.name for f in Path(output_root).glob("*.htm*")
                                if f.is_file() and not f.name.startswith("_")
                            )
                            if existing:
                                hint_parts.append(
                                    "Existing HTML files: " + ", ".join(existing[:10]) + "."
                                )
                        except Exception:
                            pass
                    if output_root:
                        hint_parts.append(
                            f'Example: {{"action": "write", "path": "{output_root}/index.html", '
                            '"content": "<!DOCTYPE html>..."}}'
                        )
                    hint = (" " + " ".join(hint_parts)) if hint_parts else ""
                    return PluginResult(
                        success=False,
                        error=f"Blank path is not allowed for file_ops action '{action}'.{hint}",
                    )
                if not str(path or "").strip():
                    hint_parts = []
                    if output_root:
                        hint_parts.append(f"Use a path under {output_root}.")
                    if allowed_html_targets:
                        hint_parts.append(
                            "Allowed HTML filenames: " + ", ".join(allowed_html_targets) + "."
                        )
                    elif output_root:
                        try:
                            existing = sorted(
                                f.name for f in Path(output_root).glob("*.htm*")
                                if f.is_file() and not f.name.startswith("_")
                            )
                            if existing:
                                hint_parts.append(
                                    "Existing HTML files: " + ", ".join(existing[:10]) + "."
                                )
                        except Exception:
                            pass
                    if output_root:
                        hint_parts.append(
                            f'Example: {{"action": "write", "path": "{output_root}/index.html", '
                            '"content": "<!DOCTYPE html>..."}}'
                        )
                    hint = (" " + " ".join(hint_parts)) if hint_parts else ""
                    return PluginResult(
                        success=False,
                        error=f"Blank path is not allowed for file_ops action '{action}'.{hint}",
                    )

            stale_attempt_guard = self._enforce_active_write_token(str(action), context or {})
            if stale_attempt_guard is not None:
                return stale_attempt_guard
            output_escape_guard = self._guard_output_dir_escape(str(path), str(action), context or {})
            if output_escape_guard is not None:
                return output_escape_guard
            # V4.6 SPEED: Apply auto-corrected path if guard redirected it
            if context and context.get("_auto_corrected_path"):
                path = context.pop("_auto_corrected_path")
                os.makedirs(os.path.dirname(path), exist_ok=True)

            # Security check: validate against allowed directories
            allowed_dirs = context.get("allowed_dirs", ["/tmp"]) if context else ["/tmp"]
            if not self._is_allowed_path(path, allowed_dirs):
                return PluginResult(success=False, error=f"Path not allowed by security policy: {path}")

            builder_html_guard = self._enforce_builder_html_targets(str(action), str(path), context or {})
            if builder_html_guard is not None:
                return builder_html_guard
            shared_asset_owner_guard = self._enforce_builder_shared_root_asset_ownership(
                str(action),
                str(path),
                context or {},
            )
            if shared_asset_owner_guard is not None:
                return shared_asset_owner_guard
            html_owner_guard = self._enforce_deliverable_html_write_ownership(str(action), str(path), context or {})
            if html_owner_guard is not None:
                return html_owner_guard
            patch_read_guard = self._enforce_builder_patch_read_before_write(
                str(action),
                str(path),
                context or {},
            )
            if patch_read_guard is not None:
                return patch_read_guard

            if action == "read":
                if not os.path.exists(path):
                    return PluginResult(success=False, error=f"File not found: {path}")
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(500_000)  # Limit to 500KB
                self._record_builder_read(path, context or {})
                return PluginResult(success=True, data={
                    "path": path, "content": content, "size": os.path.getsize(path)
                })
            elif action == "write":
                content = params.get("content", "")
                allow_empty = bool(params.get("allow_empty", False))
                task_type = str((context or {}).get("task_type") or "website").strip() or "website"
                content = postprocess_generated_text(
                    str(content or ""),
                    filename=Path(str(path or "")).name,
                    task_type=task_type,
                )
                # v5.8: refuse to ship zero-byte files to the delivery folder.
                # Models sometimes call write with empty content for asset briefs,
                # leaving 0-byte placeholders the user sees in the final output.
                if not allow_empty and not str(content or "").strip():
                    return PluginResult(
                        success=False,
                        error=(
                            f"Refused to write zero-byte file: {path}. "
                            f"Provide real content for this file, or pass allow_empty=true "
                            f"if you genuinely want an empty placeholder."
                        ),
                    )
                deliverable_placeholder_guard = self._guard_deliverable_placeholder_write(
                    str(action),
                    str(path),
                    content,
                    context or {},
                )
                if deliverable_placeholder_guard is not None:
                    return deliverable_placeholder_guard
                html_integrity_guard = self._validate_deliverable_html_write(
                    str(action),
                    str(path),
                    content,
                    context or {},
                )
                if html_integrity_guard is not None:
                    return html_integrity_guard
                html_regression_guard = self._guard_builder_html_regression(
                    str(action),
                    str(path),
                    content,
                    context or {},
                )
                if html_regression_guard is not None:
                    return html_regression_guard
                shared_asset_regression_guard = self._guard_shared_asset_regression(
                    str(action),
                    str(path),
                    content,
                    context or {},
                )
                if shared_asset_regression_guard is not None:
                    return shared_asset_regression_guard
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                try:
                    materialize_local_runtime_assets(Path(path), task_type=task_type)
                except Exception as runtime_exc:
                    logger.warning("Failed to materialize local runtime assets for %s: %s", path, runtime_exc)
                return PluginResult(success=True, data={
                    "path": path, "size": len(content), "written": True
                })
            elif action == "list":
                if not os.path.isdir(path):
                    return PluginResult(success=False, error=f"Not a directory: {path}")
                entries = []
                for entry in os.scandir(path):
                    entries.append({
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "size": entry.stat().st_size if entry.is_file() else 0
                    })
                return PluginResult(success=True, data={"path": path, "entries": entries[:200]})
            elif action == "patch":
                # v6.1.6 (Aider-style udiff): apply a unified diff to the file.
                # Intended for debugger/polisher surgical edits — avoids
                # rewriting the whole file to change a few lines.
                patch_text = params.get("patch") or params.get("diff", "")
                if not patch_text.strip():
                    return PluginResult(success=False, error="patch action requires 'patch' (unified diff) param")
                # Opus R2 fix: support /dev/null new-file creation. If the
                # patch header says --- /dev/null, skip the existence guard.
                creates_new_file = False
                for head_line in patch_text.splitlines()[:5]:
                    if head_line.startswith("---") and "/dev/null" in head_line:
                        creates_new_file = True
                        break
                if not creates_new_file and not os.path.exists(path):
                    return PluginResult(success=False, error=f"File not found: {path}")
                original = ""
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        original = f.read()
                try:
                    patched = _apply_unified_diff(original, patch_text)
                except _PatchApplyError as exc:
                    return PluginResult(success=False, error=f"patch failed: {exc}")
                if patched == original:
                    return PluginResult(success=False, error="patch produced no changes")
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(patched)
                return PluginResult(success=True, data={
                    "path": path, "size": len(patched), "patched": True,
                    "created": creates_new_file and original == "",
                    "bytes_changed": abs(len(patched) - len(original)),
                    # v7.30 (maintainer 2026-04-29): mark patch as a write so
                    # _tool_result_has_write detects it (parallel to edit fix).
                    "written": True,
                    "bytes_written": len(patched.encode("utf-8")),
                })
            elif action == "edit":
                # Claude Code-style diff edit: replace old_string with new_string
                old_string = params.get("old_string", "")
                new_string = params.get("new_string", "")
                if not old_string:
                    return PluginResult(success=False, error="edit requires 'old_string'")
                if not os.path.exists(path):
                    return PluginResult(success=False, error=f"File not found: {path}")
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                count = content.count(old_string)
                if count == 0:
                    # v7.31 (maintainer 2026-04-29): instead of bare "not found",
                    # find the closest matching line + emit a 6-line context
                    # window so the LLM can correct the anchor on the next
                    # turn. Without this hint, patcher LLMs (esp. kimi) tend
                    # to retry the same wrong anchor 6+ times before giving
                    # up — observed in run_28ec559d3311 where reviewer flagged
                    # animation issues but patcher's edits all missed because
                    # polisher had altered the anchor lines minutes earlier.
                    _hint = ""
                    try:
                        old_first_line = (old_string.split("\n", 1)[0] or "").strip()
                        if old_first_line and 8 <= len(old_first_line) <= 200:
                            file_lines = content.splitlines()
                            best_idx = -1
                            best_score = 0
                            # Cheap substring overlap: find line with most shared chars
                            old_set = set(old_first_line)
                            for i, ln in enumerate(file_lines):
                                if not ln.strip():
                                    continue
                                # Quick bigram/literal substring check
                                if old_first_line[:30] in ln or any(t in ln for t in old_first_line.split() if len(t) >= 6):
                                    score = len(old_set & set(ln))
                                    if score > best_score:
                                        best_score = score
                                        best_idx = i
                            if best_idx >= 0:
                                lo = max(0, best_idx - 3)
                                hi = min(len(file_lines), best_idx + 4)
                                window = "\n".join(f"{n+1:>4}: {file_lines[n]}" for n in range(lo, hi))
                                _hint = (
                                    f" Closest match around line {best_idx+1}:\n{window}\n"
                                    f"Hint: re-emit `file_ops edit` with `old_string` copied verbatim "
                                    f"from the lines above (escape \\n if multi-line)."
                                )
                    except Exception:
                        pass
                    return PluginResult(
                        success=False,
                        error=f"old_string not found in file.{_hint}",
                    )
                if count > 1 and not params.get("replace_all", False):
                    return PluginResult(success=False, error=f"old_string matches {count} locations; set replace_all=true or provide more context")
                if params.get("replace_all", False):
                    new_content = content.replace(old_string, new_string)
                else:
                    new_content = content.replace(old_string, new_string, 1)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                # v7.30 (maintainer 2026-04-29): include written/bytes_written so
                # _tool_result_has_write recognizes edits as writes. Without
                # these fields, patcher's `file_ops edit` calls registered as
                # 0 writes — the orchestrator counted 6 successful tool_calls
                # as files=0 and failed the patcher round.
                return PluginResult(success=True, data={
                    "path": path,
                    "replacements": count if params.get("replace_all") else 1,
                    "written": True,
                    "bytes_written": len(new_content.encode("utf-8")),
                    "size": len(new_content),
                })
            elif action == "search":
                # Grep/glob hybrid: search file contents or find files by pattern
                pattern = params.get("pattern", "")
                if not pattern:
                    return PluginResult(success=False, error="search requires 'pattern'")
                import glob as glob_mod
                search_path = path or "."
                if os.path.isdir(search_path):
                    # Glob for files, then grep inside them
                    file_glob = params.get("glob", "**/*")
                    matches = []
                    files_scanned = 0
                    max_files = 5000
                    for fpath in glob_mod.iglob(os.path.join(search_path, file_glob), recursive=True):
                        if not os.path.isfile(fpath):
                            continue
                        files_scanned += 1
                        if files_scanned > max_files:
                            break
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                for lineno, line in enumerate(f, 1):
                                    if pattern in line:
                                        matches.append({"file": fpath, "line": lineno, "text": line.rstrip()[:200]})
                                        if len(matches) >= 100:
                                            break
                        except (OSError, UnicodeDecodeError):
                            continue
                        if len(matches) >= 100:
                            break
                    return PluginResult(success=True, data={"pattern": pattern, "matches": matches, "count": len(matches)})
                elif os.path.isfile(search_path):
                    matches = []
                    with open(search_path, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if pattern in line:
                                matches.append({"line": lineno, "text": line.rstrip()[:200]})
                                if len(matches) >= 100:
                                    break
                    return PluginResult(success=True, data={"file": search_path, "matches": matches, "count": len(matches)})
                else:
                    return PluginResult(success=False, error=f"Path not found: {search_path}")
            elif action == "delete":
                # V4.9.7 FIX: Apply same security checks as write/edit before deleting.
                if not self._is_allowed_path(path, allowed_dirs):
                    return PluginResult(success=False, error=f"Delete blocked — path not allowed by security policy: {path}")
                delete_escape_guard = self._guard_output_dir_escape(str(path), "delete", context or {})
                if delete_escape_guard is not None:
                    return delete_escape_guard
                if os.path.exists(path):
                    os.remove(path)
                return PluginResult(success=True, data={"deleted": path})
            else:
                return PluginResult(success=False, error=f"Unknown action: {action}")
        except Exception as e:
            return PluginResult(success=False, error=str(e))

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "write", "edit", "search", "list", "delete"]},
                "path": {"type": "string", "description": "File or directory path"},
                "content": {"type": "string", "description": "Content to write (for write action)"},
                "old_string": {"type": "string", "description": "Text to find and replace (for edit action)"},
                "new_string": {"type": "string", "description": "Replacement text (for edit action)"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences (for edit action, default false)"},
                "pattern": {"type": "string", "description": "Search pattern (for search action)"},
                "glob": {"type": "string", "description": "File glob filter for directory search (default **/*)"}
            },
            "required": ["action", "path"]
        }


# ─────────────────────────────────────────────
# 4. ComfyUI Plugin
# ─────────────────────────────────────────────
class ComfyUIPlugin(Plugin):
    name = "comfyui"
    display_name = "ComfyUI"
    description = "Check a ComfyUI image-generation backend and optionally render assets from a configured workflow template"
    icon = "fa-image"
    security_level = SecurityLevel.L2

    def _base_url(self, context: Dict[str, Any] | None = None) -> str:
        if isinstance(context, dict):
            for key in ("comfyui_base_url", "comfyui_url"):
                value = str(context.get(key, "") or "").strip()
                if value:
                    return value.rstrip("/")
        return str(os.getenv("EVERMIND_COMFYUI_URL", "http://127.0.0.1:8188") or "http://127.0.0.1:8188").rstrip("/")

    def _workflow_template_path(self, context: Dict[str, Any] | None = None) -> str:
        if isinstance(context, dict):
            for key in ("comfyui_workflow_template", "comfyui_template_path"):
                value = str(context.get(key, "") or "").strip()
                if value:
                    return value
        return str(os.getenv("EVERMIND_COMFYUI_WORKFLOW_TEMPLATE", "") or "").strip()

    def _http_json(self, method: str, url: str, payload: Dict[str, Any] | None = None, timeout: int = 15) -> Dict[str, Any]:
        body = None
        headers = {"Content-Type": "application/json", "User-Agent": "Evermind/2.1"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request = Request(url, data=body, headers=headers, method=method.upper())
        with urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}

    def _http_bytes(self, url: str, timeout: int = 30) -> bytes:
        request = Request(url, headers={"User-Agent": "Evermind/2.1"}, method="GET")
        with urlopen(request, timeout=timeout) as resp:
            return resp.read()

    def _load_template_workflow(self, path: str) -> Dict[str, Any]:
        workflow_path = Path(path).expanduser()
        raw = workflow_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("ComfyUI workflow template must be a JSON object")
        return payload

    def _apply_placeholders(self, value: Any, replacements: Dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {k: self._apply_placeholders(v, replacements) for k, v in value.items()}
        if isinstance(value, list):
            return [self._apply_placeholders(item, replacements) for item in value]
        if isinstance(value, str):
            rendered = value
            for key, replacement in replacements.items():
                rendered = rendered.replace(f"{{{{{key}}}}}", str(replacement))
            return rendered
        return value

    def _extract_images(self, history_payload: Dict[str, Any]) -> List[Dict[str, str]]:
        outputs = history_payload.get("outputs")
        if not isinstance(outputs, dict):
            return []
        images: List[Dict[str, str]] = []
        for node_result in outputs.values():
            if not isinstance(node_result, dict):
                continue
            for image in node_result.get("images") or []:
                if not isinstance(image, dict):
                    continue
                filename = str(image.get("filename", "")).strip()
                if not filename:
                    continue
                images.append({
                    "filename": filename,
                    "subfolder": str(image.get("subfolder", "") or ""),
                    "type": str(image.get("type", "output") or "output"),
                })
        return images

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        action = str(params.get("action", "health") or "health").strip().lower()
        base_url = self._base_url(context)
        try:
            if action == "health":
                data = await asyncio.to_thread(self._http_json, "GET", f"{base_url}/system_stats")
                return PluginResult(success=True, data={"base_url": base_url, "reachable": True, "system_stats": data})

            if action != "render_workflow":
                return PluginResult(success=False, error=f"Unknown action: {action}")

            workflow: Dict[str, Any] | None = None
            workflow_param = params.get("workflow")
            if isinstance(workflow_param, dict):
                workflow = workflow_param
            elif isinstance(workflow_param, str) and workflow_param.strip():
                parsed = json.loads(workflow_param)
                if isinstance(parsed, dict):
                    workflow = parsed
            if workflow is None:
                template_path = self._workflow_template_path(context)
                if not template_path:
                    return PluginResult(
                        success=False,
                        error="No ComfyUI workflow or EVERMIND_COMFYUI_WORKFLOW_TEMPLATE configured",
                    )
                workflow = await asyncio.to_thread(self._load_template_workflow, template_path)

            replacements = {
                "POSITIVE_PROMPT": str(params.get("prompt", "") or ""),
                "NEGATIVE_PROMPT": str(params.get("negative_prompt", "") or ""),
                "SEED": int(params.get("seed", 0) or 0),
                "WIDTH": int(params.get("width", 1024) or 1024),
                "HEIGHT": int(params.get("height", 1024) or 1024),
                "STEPS": int(params.get("steps", 30) or 30),
                "CFG": float(params.get("cfg", 7.0) or 7.0),
            }
            rendered_workflow = self._apply_placeholders(workflow, replacements)
            client_id = str(params.get("client_id") or f"evermind-{int(time.time())}")
            queue_resp = await asyncio.to_thread(
                self._http_json,
                "POST",
                f"{base_url}/prompt",
                {"prompt": rendered_workflow, "client_id": client_id},
                30,
            )
            prompt_id = str(queue_resp.get("prompt_id", "") or "")
            if not prompt_id:
                return PluginResult(success=False, error=f"ComfyUI did not return prompt_id: {queue_resp}")

            timeout_sec = max(15, min(int(params.get("timeout_sec", 180) or 180), 600))
            deadline = time.time() + timeout_sec
            history_entry: Dict[str, Any] = {}
            while time.time() < deadline:
                history = await asyncio.to_thread(self._http_json, "GET", f"{base_url}/history/{prompt_id}", None, 30)
                history_entry = history.get(prompt_id) if isinstance(history.get(prompt_id), dict) else {}
                if history_entry and self._extract_images(history_entry):
                    break
                await asyncio.sleep(2)

            image_entries = self._extract_images(history_entry)
            if not image_entries:
                return PluginResult(
                    success=False,
                    error=f"ComfyUI workflow finished without image outputs within {timeout_sec}s",
                    data={"prompt_id": prompt_id, "base_url": base_url},
                )

            output_dir = Path(str((context or {}).get("output_dir", "/tmp/evermind_output")))
            save_dir = Path(str(params.get("save_dir") or (output_dir / "generated_assets")))
            save_dir.mkdir(parents=True, exist_ok=True)

            artifacts: List[Dict[str, Any]] = []
            saved_files: List[str] = []
            for index, image in enumerate(image_entries, start=1):
                query = urlencode({
                    "filename": image["filename"],
                    "subfolder": image["subfolder"],
                    "type": image["type"],
                })
                image_bytes = await asyncio.to_thread(self._http_bytes, f"{base_url}/view?{query}", 60)
                ext = Path(image["filename"]).suffix or ".png"
                target = save_dir / f"{prompt_id}_{index}{ext}"
                target.write_bytes(image_bytes)
                saved_files.append(str(target))
                artifacts.append({"type": "image", "path": str(target)})

            return PluginResult(
                success=True,
                data={
                    "base_url": base_url,
                    "prompt_id": prompt_id,
                    "saved_files": saved_files,
                    "image_count": len(saved_files),
                },
                artifacts=artifacts,
            )
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            return PluginResult(success=False, error=str(exc), data={"base_url": base_url})
        except Exception as exc:
            return PluginResult(success=False, error=str(exc), data={"base_url": base_url})

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["health", "render_workflow"]},
                "workflow": {
                    "description": "Optional ComfyUI workflow JSON object or JSON string. If omitted, EVERMIND_COMFYUI_WORKFLOW_TEMPLATE is used.",
                },
                "prompt": {"type": "string", "description": "Positive prompt injected into the workflow template."},
                "negative_prompt": {"type": "string", "description": "Negative prompt injected into the workflow template."},
                "seed": {"type": "integer"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "steps": {"type": "integer"},
                "cfg": {"type": "number"},
                "timeout_sec": {"type": "integer"},
                "save_dir": {"type": "string"},
            },
            "required": ["action"],
        }


# ─────────────────────────────────────────────
# 4c. Video Review Plugin (v6.2 — maintainer 2026-04-20)
# ─────────────────────────────────────────────
# Wraps backend.video_review.VideoReview. Reviewer calls this AFTER using
# browser.record_preview to obtain a webm clip, then feeds the path back
# here for a vision-model judgment.
class VideoReviewPlugin(Plugin):
    name = "video_review"
    display_name = "Video Review"
    description = "Send a short gameplay/animation webm to a vision LLM (qwen-vl-max / doubao-vision / gemini) for structured QA verdict"
    icon = "fa-film"
    security_level = SecurityLevel.L2

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        action = str(params.get("action") or "judge").strip().lower()
        runtime_config = (context or {}).get("runtime_config") or (context or {}).get("settings") or {}
        try:
            from video_review import VideoReview  # local import
        except Exception as exc:
            return PluginResult(success=False, error=f"video_review adapter unavailable: {exc}")

        reviewer = VideoReview(runtime_config if isinstance(runtime_config, dict) else {})

        if action in ("health", "status"):
            return PluginResult(success=True, data={
                "available": reviewer.available,
                "provider": reviewer.provider,
                "model": reviewer.model,
            })

        if action != "judge":
            return PluginResult(success=False, error=f"Unknown action: {action}")

        if not reviewer.available:
            return PluginResult(
                success=False,
                error="VideoReview not configured (no vision model key). Reviewer should fall back to screenshot flow.",
                data={"degraded": True},
            )

        video_path = str(params.get("video_path") or "").strip()
        if not video_path:
            return PluginResult(success=False, error="judge requires video_path")

        task_type = str(params.get("task_type") or "game").strip().lower()
        goal = str(params.get("goal") or "").strip()
        rubric = params.get("custom_rubric")
        custom_rubric = [str(x) for x in rubric] if isinstance(rubric, list) else None

        try:
            verdict = await reviewer.judge(
                video_path=video_path,
                task_type=task_type,
                goal=goal,
                custom_rubric=custom_rubric,
            )
        except Exception as exc:
            return PluginResult(success=False, error=f"judge exception: {exc}")

        if not verdict:
            return PluginResult(success=False, error="Video review returned no verdict")

        return PluginResult(success=True, data={
            "verdict": verdict,
            "provider": verdict.get("provider", reviewer.provider),
            "pass": bool(verdict.get("pass", False)),
            "confidence": verdict.get("confidence"),
            "issues": verdict.get("issues", []),
        })

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["judge", "health"], "default": "judge"},
                "video_path": {"type": "string", "description": "Absolute path to .webm file (usually from browser.record_preview)"},
                "task_type": {"type": "string", "enum": ["game", "website", "slides", "landing", "portfolio", "presentation", "other"], "default": "game"},
                "goal": {"type": "string", "description": "Project goal for context (e.g. '3D first-person shooter')"},
                "custom_rubric": {"type": "array", "items": {"type": "string"}, "description": "Extra yes/no checks to prepend to default rubric"},
            },
            "required": ["action"],
        }


# ─────────────────────────────────────────────
# 4b. Image Generation Plugin (v6.2 — maintainer 2026-04-20)
# ─────────────────────────────────────────────
# Wraps backend.image_gen.ImageGen so the `imagegen` node can call a real
# text-to-image API (tongyi / doubao / seedream / flux-fal / openai-compat).
# Returns paths to WebP files written to /tmp/evermind_output/assets/.
# Degrades gracefully: no-key / unknown-provider → success=False with
# a descriptive error, orchestrator keeps SVG fallback behaviour.
class ImageGenPlugin(Plugin):
    name = "image_gen"
    display_name = "Image Generation"
    description = "Generate images via tongyi / doubao / seedream / flux-fal / openai-compat providers (returns WebP files)"
    icon = "fa-image"
    security_level = SecurityLevel.L2

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        action = str(params.get("action") or "generate").strip().lower()
        runtime_config = (context or {}).get("runtime_config") or (context or {}).get("settings") or {}
        try:
            from image_gen import ImageGen  # local import to avoid circular
        except Exception as exc:
            return PluginResult(success=False, error=f"image_gen adapter unavailable: {exc}")

        gen = ImageGen(runtime_config if isinstance(runtime_config, dict) else {})

        if action in ("health", "status"):
            return PluginResult(success=True, data={
                "available": gen.available,
                "provider": gen.provider,
                "default_model": gen.default_model,
                "default_size": gen.default_size,
                "max_images_per_run": gen.max_images,
            })

        if action != "generate":
            return PluginResult(success=False, error=f"Unknown action: {action}")

        if not gen.available:
            return PluginResult(
                success=False,
                error="ImageGen not configured (missing provider+api_key). Degrading to SVG fallback.",
                data={"degraded": True, "provider": gen.provider},
            )

        prompt = str(params.get("prompt") or "").strip()
        slug = str(params.get("slug") or params.get("output_slug") or "image").strip() or "image"
        size = str(params.get("size") or gen.default_size or "1024x1024").strip()
        model = str(params.get("model") or gen.default_model or "").strip() or None

        if len(prompt) < 3:
            return PluginResult(success=False, error="Prompt too short (< 3 chars)")

        try:
            result = await gen.generate(prompt=prompt, output_slug=slug, size=size, model=model)
        except Exception as exc:
            return PluginResult(success=False, error=f"Generation exception: {exc}")

        if not result or result.get("status") != "ok":
            return PluginResult(success=False, error="Generation failed (provider returned no image)")

        files = result.get("files") or {}
        artifacts = [{"type": "image", "path": p} for p in files.values() if p]
        return PluginResult(success=True, data={
            "provider": result.get("provider"),
            "files": files,
            "cached": result.get("cached", False),
            "prompt": prompt,
            "slug": slug,
            "size": size,
        }, artifacts=artifacts)

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["generate", "health"], "default": "generate"},
                "prompt": {"type": "string", "description": "Text prompt for image generation"},
                "slug": {"type": "string", "description": "Output filename slug (e.g. 'hero', 'sprite-slime')"},
                "size": {"type": "string", "description": "WxH, e.g. 1024x1024 / 1920x1080"},
                "model": {"type": "string", "description": "Override provider's default model"},
            },
            "required": ["action"],
        }


# ─────────────────────────────────────────────
# 5. Shell Plugin
# ─────────────────────────────────────────────
class ShellPlugin(Plugin):
    name = "shell"
    display_name = "Shell"
    description = "Execute shell/terminal commands with timeout and safety controls"
    icon = "fa-terminal"
    security_level = SecurityLevel.L3

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            command = params.get("command", "")
            cwd = params.get("cwd", context.get("workspace", "/tmp") if context else "/tmp")
            timeout = min(params.get("timeout", 30), context.get("max_timeout", 60) if context else 60)

            # Security: block dangerous commands
            blocked = ["rm -rf /", "sudo rm", "mkfs", ": () {", "dd if="]
            if any(b in command for b in blocked):
                return PluginResult(success=False, error="Command blocked by security policy")

            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return PluginResult(success=False, error=f"Command timed out after {timeout}s")

            return PluginResult(
                success=proc.returncode == 0,
                data={
                    "stdout": stdout.decode("utf-8", errors="replace")[:50000],
                    "stderr": stderr.decode("utf-8", errors="replace")[:10000],
                    "returncode": proc.returncode,
                    "command": command
                },
                error=stderr.decode("utf-8", errors="replace")[:5000] if proc.returncode != 0 else None
            )
        except Exception as e:
            return PluginResult(success=False, error=str(e))

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {"type": "string", "description": "Working directory"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (max 60)"}
            },
            "required": ["command"]
        }


# ─────────────────────────────────────────────
# 5. Git Plugin
# ─────────────────────────────────────────────
class GitPlugin(Plugin):
    name = "git"
    display_name = "Git"
    description = "Git version control operations: status, diff, commit, push, pull"
    icon = "fa-code-branch"
    security_level = SecurityLevel.L2

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            action = params.get("action", "status")
            repo_path = params.get("repo_path", context.get("workspace", ".") if context else ".")

            cmd_map = {
                "status": "git status --porcelain",
                "diff": "git diff",
                "log": "git log --oneline -20",
                "add": f"git add {params.get('files', '.')}",
                "commit": f"git commit -m \"{params.get('message', 'Auto commit')}\"",
                "push": "git push",
                "pull": "git pull",
                "branch": "git branch -a",
                "stash": "git stash",
            }

            cmd = cmd_map.get(action)
            if not cmd:
                return PluginResult(success=False, error=f"Unknown git action: {action}")

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            return PluginResult(
                success=proc.returncode == 0,
                data={
                    "output": stdout.decode("utf-8", errors="replace")[:30000],
                    "action": action,
                    "command": cmd
                },
                error=stderr.decode("utf-8", errors="replace") if proc.returncode != 0 else None
            )
        except Exception as e:
            return PluginResult(success=False, error=str(e))

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "diff", "log", "add", "commit", "push", "pull", "branch", "stash"]},
                "repo_path": {"type": "string"},
                "files": {"type": "string", "description": "Files to add (for 'add' action)"},
                "message": {"type": "string", "description": "Commit message (for 'commit' action)"}
            },
            "required": ["action"]
        }


# ─────────────────────────────────────────────
# 6. Browser Use Plugin
# ─────────────────────────────────────────────
class BrowserUsePlugin(Plugin):
    name = "browser_use"
    display_name = "Browser Use"
    description = (
        "High-level agentic browser automation for multi-step interactive QA such as web apps, "
        "browser games, and click-heavy flows. Prefer this for actual play / multi-action interaction, "
        "then use the normal browser tool to capture explicit verification evidence."
    )
    icon = "fa-gamepad"
    security_level = SecurityLevel.L2

    def _runner_python(self, context: Dict[str, Any] | None = None) -> str:
        ctx = context if isinstance(context, dict) else {}
        configured = str(ctx.get("browser_use_python") or os.getenv("EVERMIND_BROWSER_USE_PYTHON", "")).strip()
        if configured:
            return configured
        sidecar = Path.home() / ".evermind" / "browser_use_venv" / "bin" / "python"
        if sidecar.exists():
            return str(sidecar)
        return sys.executable

    def _runner_script(self) -> Path:
        return Path(__file__).resolve().parents[1] / "scripts" / "browser_use_runner.py"

    def _artifact_dir(self, context: Dict[str, Any] | None = None) -> Path:
        ctx = context if isinstance(context, dict) else {}
        output_dir = Path(str(ctx.get("output_dir") or "/tmp/evermind_output")).expanduser()
        run_id = str(ctx.get("run_id") or "run").strip() or "run"
        node_execution_id = str(ctx.get("node_execution_id") or ctx.get("node_type") or "browser_use").strip() or "browser_use"
        artifact_dir = output_dir / "_browser_use" / run_id / node_execution_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        task = str(params.get("task") or params.get("instruction") or "").strip()
        url = str(params.get("url") or "").strip()
        if not task and not url:
            return PluginResult(success=False, error="browser_use requires a task or url")

        runner_python = self._runner_python(context)
        runner_script = self._runner_script()
        if not runner_script.exists():
            return PluginResult(success=False, error=f"browser_use runner script missing: {runner_script}")

        ctx = dict(context or {})
        openai_key = (
            str(ctx.get("openai_api_key") or "").strip()
            or str(((ctx.get("api_keys") or {}) if isinstance(ctx.get("api_keys"), dict) else {}).get("openai") or "").strip()
            or str(os.getenv("OPENAI_API_KEY", "") or "").strip()
        )
        if not openai_key:
            return PluginResult(success=False, error="browser_use requires an OpenAI-compatible API key")

        openai_base = (
            str(ctx.get("openai_api_base") or "").strip()
            or str(((ctx.get("api_bases") or {}) if isinstance(ctx.get("api_bases"), dict) else {}).get("openai") or "").strip()
            or str(os.getenv("OPENAI_BASE_URL", "") or "").strip()
        )

        requested_headful = bool(ctx.get("browser_headful"))
        artifact_dir = self._artifact_dir(ctx)
        payload = {
            "task": task,
            "url": url,
            "model": str(params.get("model") or ctx.get("browser_use_model") or ctx.get("default_model") or "gpt-4o").strip(),
            "headless": not requested_headful,
            "max_steps": max(2, min(int(params.get("max_steps", 10) or 10), 40)),
            "use_vision": bool(params.get("use_vision", True)),
            "artifact_dir": str(artifact_dir),
            "recordings_dir": str(artifact_dir / "recordings"),
            "screenshots_dir": str(artifact_dir / "screenshots"),
        }

        env = os.environ.copy()
        env["OPENAI_API_KEY"] = openai_key
        if openai_base:
            env["OPENAI_BASE_URL"] = openai_base
        env.setdefault("BROWSER_USE_CONFIG_DIR", str(Path.home() / ".evermind" / "browser_use_config"))

        try:
            proc = await asyncio.create_subprocess_exec(
                runner_python,
                str(runner_script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except Exception as exc:
            return PluginResult(success=False, error=f"browser_use runner launch failed: {str(exc)[:240]}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload).encode("utf-8")),
                timeout=max(30, min(int(params.get("timeout_sec", 180) or 180), 600)),
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return PluginResult(success=False, error="browser_use runner timed out")

        stderr_text = (stderr or b"").decode("utf-8", errors="ignore").strip()
        stdout_text = (stdout or b"").decode("utf-8", errors="ignore").strip()
        if proc.returncode != 0:
            return PluginResult(
                success=False,
                error=f"browser_use runner failed ({proc.returncode}): {(stderr_text or stdout_text)[:400]}",
            )

        try:
            result = json.loads(stdout_text) if stdout_text else {}
        except Exception:
            return PluginResult(success=False, error=f"browser_use returned invalid JSON: {stdout_text[:300]}")

        artifacts = list(result.get("artifacts") or [])
        data = dict(result.get("data") or {})
        data.setdefault("browser_mode", "headful" if requested_headful else "headless")
        data.setdefault("requested_mode", "headful" if requested_headful else "headless")
        if stderr_text and "runner_note" not in data:
            data["runner_note"] = stderr_text[:300]
        return PluginResult(
            success=bool(result.get("success", False)),
            data=data,
            error=str(result.get("error") or "").strip() or None,
            artifacts=artifacts,
        )

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "High-level interactive objective for the browser-use agent"},
                "instruction": {"type": "string", "description": "Alias of task"},
                "url": {"type": "string", "description": "Starting URL to open before interaction"},
                "model": {"type": "string", "description": "OpenAI-compatible model id for the browser-use agent"},
                "max_steps": {"type": "integer", "description": "Maximum browser agent steps", "default": 10},
                "timeout_sec": {"type": "integer", "description": "Hard timeout for the sidecar run", "default": 180},
                "use_vision": {"type": "boolean", "description": "Allow visual reasoning if the sidecar supports it", "default": True},
            },
        }


# ─────────────────────────────────────────────
# 7. Computer Use Plugin (GPT-5.4 CUA)
# ─────────────────────────────────────────────
class ComputerUsePlugin(Plugin):
    name = "computer_use"
    display_name = "Computer Use (CUA)"
    description = "GPT-5.4 native computer use — control screen via screenshots, mouse, and keyboard"
    icon = "fa-desktop"
    security_level = SecurityLevel.L3

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            from openai import AsyncOpenAI

            api_key = context.get("openai_api_key") if context else os.getenv("OPENAI_API_KEY")
            if not api_key:
                return PluginResult(success=False, error="OpenAI API key not configured")

            # V4.9.5 PERF: Reuse cached client with proper connection pool
            # instead of creating a new one per request (default keepalive=5s).
            _cache_key = f"cua_{api_key[:20]}"
            if not hasattr(AsyncOpenAI, '_evermind_client_cache'):
                AsyncOpenAI._evermind_client_cache = {}
            client = AsyncOpenAI._evermind_client_cache.get(_cache_key)
            if client is None:
                try:
                    import httpx as _httpx
                    _hc = _httpx.AsyncClient(
                        http2=True,
                        timeout=_httpx.Timeout(120.0, connect=10.0),
                        limits=_httpx.Limits(max_connections=10, max_keepalive_connections=6, keepalive_expiry=120),
                    )
                    client = AsyncOpenAI(api_key=api_key, http_client=_hc)
                except Exception:
                    client = AsyncOpenAI(api_key=api_key)
                AsyncOpenAI._evermind_client_cache[_cache_key] = client

            instruction = params.get("instruction", "")
            display_w = params.get("display_width", 1920)
            display_h = params.get("display_height", 1080)
            environment = params.get("environment", "browser")

            # Build the CUA request
            tools = [{
                "type": "computer_use_preview",
                "display_width": display_w,
                "display_height": display_h,
                "environment": environment
            }]

            response = await client.responses.create(
                model="computer-use-preview",
                tools=tools,
                input=[{
                    "role": "user",
                    "content": instruction
                }],
                truncation="auto"
            )

            # Extract results
            output_text = ""
            artifacts = []
            for item in response.output:
                if hasattr(item, "type"):
                    if item.type == "text":
                        output_text += item.text
                    elif item.type == "computer_call":
                        # CUA action taken (click, type, screenshot, etc.)
                        artifacts.append({
                            "type": "cua_action",
                            "action": item.action.type if hasattr(item, "action") else "unknown",
                            "data": str(item)
                        })

            return PluginResult(
                success=True,
                data={"instruction": instruction, "output": output_text, "model": "computer-use-preview"},
                artifacts=artifacts
            )
        except Exception as e:
            return PluginResult(success=False, error=f"CUA error: {str(e)}")

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "What to do on the computer"},
                "display_width": {"type": "integer", "default": 1920},
                "display_height": {"type": "integer", "default": 1080},
                "environment": {"type": "string", "enum": ["browser", "mac", "windows"], "default": "browser"}
            },
            "required": ["instruction"]
        }


# ─────────────────────────────────────────────
# 8. UI Control Plugin
# ─────────────────────────────────────────────
class UIControlPlugin(Plugin):
    name = "ui_control"
    display_name = "UI Control"
    description = "Control mouse (click, drag, double-click, right-click), keyboard, scroll, clipboard, and window management"
    icon = "fa-arrow-pointer"
    security_level = SecurityLevel.L3

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            import pyautogui
            pyautogui.FAILSAFE = True  # Move mouse to corner to abort

            action = params.get("action", "click")

            if action == "click":
                x, y = params.get("x", 0), params.get("y", 0)
                button = params.get("button", "left")
                pyautogui.click(x, y, button=button)
                return PluginResult(success=True, data={"clicked": [x, y], "button": button})

            elif action == "double_click":
                x, y = params.get("x", 0), params.get("y", 0)
                pyautogui.doubleClick(x, y)
                return PluginResult(success=True, data={"double_clicked": [x, y]})

            elif action == "right_click":
                x, y = params.get("x", 0), params.get("y", 0)
                pyautogui.rightClick(x, y)
                return PluginResult(success=True, data={"right_clicked": [x, y]})

            elif action == "triple_click":
                x, y = params.get("x", 0), params.get("y", 0)
                pyautogui.tripleClick(x, y)
                return PluginResult(success=True, data={"triple_clicked": [x, y]})

            elif action == "drag":
                from_x, from_y = params.get("from_x", 0), params.get("from_y", 0)
                to_x, to_y = params.get("to_x", 0), params.get("to_y", 0)
                duration = params.get("duration", 0.5)
                pyautogui.moveTo(from_x, from_y)
                pyautogui.drag(to_x - from_x, to_y - from_y, duration=duration)
                return PluginResult(success=True, data={"dragged": {"from": [from_x, from_y], "to": [to_x, to_y]}})

            elif action == "type":
                text = params.get("text", "")
                interval = params.get("interval", 0.02)
                pyautogui.typewrite(text, interval=interval)
                return PluginResult(success=True, data={"typed": text[:100]})

            elif action == "hotkey":
                keys = params.get("keys", [])
                pyautogui.hotkey(*keys)
                return PluginResult(success=True, data={"hotkey": keys})

            elif action == "move":
                x, y = params.get("x", 0), params.get("y", 0)
                pyautogui.moveTo(x, y, duration=0.3)
                return PluginResult(success=True, data={"moved_to": [x, y]})

            elif action == "scroll":
                amount = params.get("amount", -3)
                x, y = params.get("x", None), params.get("y", None)
                pyautogui.scroll(amount, x=x, y=y)
                return PluginResult(success=True, data={"scrolled": amount})

            elif action == "clipboard_copy":
                import subprocess
                text = params.get("text", "")
                process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                process.communicate(text.encode("utf-8"))
                return PluginResult(success=True, data={"copied": text[:100]})

            elif action == "clipboard_paste":
                import subprocess
                result = subprocess.run(["pbpaste"], capture_output=True, text=True)
                return PluginResult(success=True, data={"clipboard": result.stdout[:500]})

            elif action == "window_focus":
                app_name = params.get("app", "")
                # V4.9.7 FIX: Sanitize app_name to prevent osascript command injection.
                # Strip quotes, backslashes, and control characters that could escape the AppleScript string.
                app_name = re.sub(r'["\'\\\x00-\x1f]', '', str(app_name or "")).strip()
                if not app_name:
                    return PluginResult(success=False, error="window_focus requires a non-empty 'app' name")
                subprocess.run(
                    ["osascript", "-e", f'tell application "{app_name}" to activate'],
                    capture_output=True, text=True, timeout=5,
                )
                return PluginResult(success=True, data={"focused": app_name})

            elif action == "window_minimize":
                os.system('osascript -e \'tell application "System Events" to set miniaturized of first window of front application to true\'')
                return PluginResult(success=True, data={"minimized": True})

            elif action == "window_maximize":
                os.system('osascript -e \'tell application "System Events" to tell front application to set bounds of front window to {0, 0, 1920, 1080}\'')
                return PluginResult(success=True, data={"maximized": True})

            elif action == "window_close":
                pyautogui.hotkey("command", "w")
                return PluginResult(success=True, data={"closed_window": True})

            elif action == "window_list":
                import subprocess
                result = subprocess.run(
                    ["osascript", "-e", 'tell application "System Events" to get name of every process whose visible is true'],
                    capture_output=True, text=True, timeout=5
                )
                apps = [a.strip() for a in result.stdout.split(",")]
                return PluginResult(success=True, data={"visible_apps": apps})

            else:
                return PluginResult(success=False, error=f"Unknown action: {action}")
        except Exception as e:
            return PluginResult(success=False, error=str(e))

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "click", "double_click", "right_click", "triple_click",
                        "drag", "type", "hotkey", "move", "scroll",
                        "clipboard_copy", "clipboard_paste",
                        "window_focus", "window_minimize", "window_maximize",
                        "window_close", "window_list",
                    ],
                },
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "from_x": {"type": "integer"}, "from_y": {"type": "integer"},
                "to_x": {"type": "integer"}, "to_y": {"type": "integer"},
                "duration": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "text": {"type": "string"},
                "keys": {"type": "array", "items": {"type": "string"}},
                "amount": {"type": "integer"},
                "app": {"type": "string"},
                "interval": {"type": "number"},
            },
            "required": ["action"],
        }


class GitHubPlugin(Plugin):
    """v6.1.6 — push Evermind artifacts to a GitHub repo.

    MVP actions: connect, status, list_repos, create_repo, push.
    Uses GitHub REST API via urllib (no PyGithub dep) + local `git` CLI for
    efficient push. Token storage: macOS Keychain → ~/.evermind/config.json
    → env var, in that order. Zero config for the user — first click opens
    the PAT creation page.
    """

    name = "github"
    display_name = "GitHub"
    description = "Create repos and push generated code to GitHub."
    icon = "fa-github"
    security_level = SecurityLevel.L3

    _CFG_PATH = Path.home() / ".evermind" / "config.json"
    _API_BASE = "https://api.github.com"

    def _token(self, context: Optional[Dict[str, Any]] = None) -> str:
        ctx = context or {}
        tok = str(ctx.get("github_token") or "").strip()
        if tok:
            return tok
        try:
            import keyring
            stored = keyring.get_password("evermind", "github_pat") or ""
            if stored:
                return stored
        except Exception:
            pass
        try:
            if self._CFG_PATH.exists():
                cfg = json.loads(self._CFG_PATH.read_text("utf-8"))
                tok = str(
                    (cfg.get("integrations") or {})
                    .get("github", {})
                    .get("pat", "")
                    or ""
                ).strip()
                if tok:
                    return tok
        except Exception:
            pass
        return str(os.getenv("GITHUB_TOKEN", "") or "").strip()

    def _store_token(self, pat: str) -> str:
        try:
            import keyring
            keyring.set_password("evermind", "github_pat", pat)
            return "keychain"
        except Exception:
            pass
        cfg: Dict[str, Any] = {}
        try:
            if self._CFG_PATH.exists():
                cfg = json.loads(self._CFG_PATH.read_text("utf-8"))
        except Exception:
            cfg = {}
        cfg.setdefault("integrations", {}).setdefault("github", {})["pat"] = pat
        self._CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
        try:
            os.chmod(self._CFG_PATH, 0o600)
        except Exception:
            pass
        return "config.json"

    def _api(self, token: str, method: str, path: str, body: Optional[Dict] = None) -> Dict:
        import urllib.request
        req = urllib.request.Request(
            f"{self._API_BASE}{path}",
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Evermind/1.0",
            },
            data=(json.dumps(body).encode("utf-8") if body is not None else None),
        )
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                msg = e.read().decode("utf-8", errors="replace")
            except Exception:
                msg = str(e)
            raise RuntimeError(f"GitHub API {e.code}: {msg[:500]}")

    async def execute(self, params: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> "PluginResult":
        action = str(params.get("action") or "").strip()
        if not action:
            return PluginResult(success=False, error="github plugin requires 'action'")

        if action == "connect":
            pat = str(params.get("pat") or "").strip()
            if not pat:
                return PluginResult(success=False, error="connect requires 'pat' (GitHub personal access token)")
            try:
                user = self._api(pat, "GET", "/user")
            except Exception as exc:
                return PluginResult(success=False, error=f"PAT verification failed: {exc}")
            stored = self._store_token(pat)
            return PluginResult(success=True, data={
                "login": user.get("login", ""),
                "stored": stored,
                "avatar_url": user.get("avatar_url", ""),
            })

        token = self._token(context)
        if not token:
            return PluginResult(
                success=False,
                error="No GitHub token. Run action='connect' with a PAT first "
                      "(create at https://github.com/settings/tokens/new?scopes=repo&description=Evermind).",
            )

        try:
            if action == "status":
                user = self._api(token, "GET", "/user")
                return PluginResult(success=True, data={"connected": True, "login": user.get("login", "")})

            if action == "list_repos":
                repos = self._api(token, "GET", "/user/repos?per_page=50&sort=updated")
                return PluginResult(success=True, data={
                    "repos": [
                        {"name": r.get("full_name", ""), "private": r.get("private", False),
                         "url": r.get("html_url", "")}
                        for r in (repos if isinstance(repos, list) else [])
                    ][:50],
                })

            if action == "create_repo":
                name = str(params.get("name") or "").strip()
                if not name:
                    return PluginResult(success=False, error="create_repo requires 'name'")
                body = {
                    "name": name,
                    "description": str(params.get("description") or "Generated by Evermind"),
                    "private": bool(params.get("private", True)),
                    "auto_init": True,
                }
                repo = self._api(token, "POST", "/user/repos", body=body)
                return PluginResult(success=True, data={
                    "full_name": repo.get("full_name", ""),
                    "url": repo.get("html_url", ""),
                    "default_branch": repo.get("default_branch", "main"),
                })

            if action == "push":
                repo_full = str(params.get("repo") or "").strip()
                if not repo_full or "/" not in repo_full:
                    return PluginResult(success=False, error="push requires 'repo' = 'owner/name'")
                src_raw = str(
                    params.get("source_dir")
                    or (context or {}).get("output_dir")
                    or "/tmp/evermind_output"
                )
                src = Path(src_raw).expanduser().resolve()
                if not src.is_dir():
                    return PluginResult(success=False, error=f"Source dir not found: {src}")
                branch = str(params.get("branch") or "main")
                message = str(params.get("message") or f"Evermind run {int(time.time())}")
                # v6.1.6 (Opus R2): clean remote URL (no token) + GIT_ASKPASS
                # to feed credentials via a one-shot script. Token never lands
                # in argv (visible to `ps`) or `.git/config`.
                remote_clean = f"https://github.com/{repo_full}.git"

                import tempfile
                askpass_fd, askpass_path = tempfile.mkstemp(prefix="evermind_ask_", suffix=".sh")
                try:
                    with os.fdopen(askpass_fd, "w") as fh:
                        # Script echoes token when git asks for "Password",
                        # and echoes "x-access-token" when asked for "Username".
                        fh.write(
                            "#!/bin/sh\n"
                            "case \"$1\" in\n"
                            "  Username*) echo 'x-access-token' ;;\n"
                            "  *) echo \"$GIT_ASKPASS_TOKEN\" ;;\n"
                            "esac\n"
                        )
                    os.chmod(askpass_path, 0o700)

                    git_env = {
                        **os.environ,
                        "GIT_TERMINAL_PROMPT": "0",
                        "GIT_ASKPASS": askpass_path,
                        "GIT_ASKPASS_TOKEN": token,
                    }

                    async def _run(*argv: str) -> tuple:
                        p = await asyncio.create_subprocess_exec(
                            *argv,
                            cwd=str(src),
                            env=git_env,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        try:
                            out, err = await asyncio.wait_for(p.communicate(), timeout=90)
                        except asyncio.TimeoutError:
                            p.kill()
                            return 124, b"", b"git command timed out"
                        return p.returncode, out, err

                    def _scrub(raw: bytes) -> str:
                        s = raw.decode("utf-8", errors="replace")
                        return s.replace(token, "***") if token else s

                    # init if needed (remote URL has NO token)
                    if not (src / ".git").exists():
                        rc, _, err = await _run("git", "init", "-b", branch)
                        if rc != 0:
                            return PluginResult(success=False, error=f"git init failed: {_scrub(err)[:400]}")
                    # configure clean remote
                    await _run("git", "remote", "remove", "origin")  # best-effort
                    rc, _, err = await _run("git", "remote", "add", "origin", remote_clean)
                    if rc != 0:
                        return PluginResult(success=False, error=f"git remote add failed: {_scrub(err)[:400]}")
                    await _run("git", "add", "-A")
                    await _run(
                        "git", "-c", "user.email=evermind@local", "-c", "user.name=Evermind",
                        "commit", "-m", message, "--allow-empty",
                    )
                    rc, out, err = await _run("git", "push", "-u", "origin", branch, "--force-with-lease")
                    if rc != 0:
                        return PluginResult(success=False, error=f"git push failed: {_scrub(err)[-1500:]}")
                    return PluginResult(success=True, data={
                        "url": f"https://github.com/{repo_full}/tree/{branch}",
                        "repo": repo_full,
                        "branch": branch,
                    })
                finally:
                    try:
                        os.unlink(askpass_path)
                    except Exception:
                        pass

            return PluginResult(success=False, error=f"Unknown action: {action}")

        except Exception as e:
            return PluginResult(success=False, error=str(e)[:1000])

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["connect", "status", "list_repos", "create_repo", "push"]},
                "pat": {"type": "string", "description": "GitHub personal access token (for connect action only)"},
                "name": {"type": "string", "description": "Repo name for create_repo"},
                "description": {"type": "string"},
                "private": {"type": "boolean", "default": True},
                "repo": {"type": "string", "description": "'owner/name' for push"},
                "source_dir": {"type": "string", "default": "/tmp/evermind_output"},
                "branch": {"type": "string", "default": "main"},
                "message": {"type": "string", "description": "Commit message"},
            },
            "required": ["action"],
        }


# ─────────────────────────────────────────────
# Auto-register all plugins
# ─────────────────────────────────────────────
def register_all():
    """Register all built-in plugins."""
    for PluginClass in [ScreenshotPlugin, BrowserPlugin, SourceFetchPlugin, BrowserUsePlugin, FileOpsPlugin, ComfyUIPlugin,
                        ImageGenPlugin, VideoReviewPlugin, ShellPlugin, GitPlugin, GitHubPlugin, ComputerUsePlugin, UIControlPlugin]:
        PluginRegistry.register(PluginClass())
    logger.info(f"Registered {len(PluginRegistry.get_all())} plugins")
