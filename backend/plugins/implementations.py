"""
Evermind Backend — Plugin Implementations
Built-in plugins: screenshot, browser, file_ops, comfyui, shell, git, computer_use, ui_control
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import Plugin, PluginResult, PluginRegistry, SecurityLevel

logger = logging.getLogger("evermind.plugins")


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
    description = "Open web pages with high-level observe/act/extract helpers plus direct click, fill, wait, and extract actions"
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
        self._bound_page_identity = None
        self._console_errors: List[Dict[str, str]] = []
        self._page_errors: List[str] = []
        self._failed_requests: List[Dict[str, str]] = []
        self._action_log: List[Dict[str, Any]] = []
        self._last_state_hash: Optional[str] = None

    def _resolve_headless(self, context: Dict[str, Any] | None = None) -> bool:
        if isinstance(context, dict) and "browser_headful" in context:
            return not bool(context.get("browser_headful"))
        env_headful = str(os.getenv("EVERMIND_BROWSER_HEADFUL", "0")).strip().lower() in ("1", "true", "yes", "on")
        return not env_headful

    async def _ensure_browser(self, context: Dict[str, Any] | None = None):
        requested_headless = self._resolve_headless(context)
        headless = requested_headless

        # Recreate browser when mode switches between headless/headful.
        if self._browser and self._headless != headless:
            await self.shutdown()

        if not self._browser:
            from playwright.async_api import async_playwright
            if not self._playwright:
                self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(headless=headless)
                self._launch_note = ""
            except Exception as launch_err:
                if headless:
                    raise
                # Fall back so workflow can continue even if GUI launch is blocked.
                logger.warning("BrowserPlugin headful launch failed; falling back to headless: %s", launch_err)
                self._browser = await self._playwright.chromium.launch(headless=True)
                headless = True
                self._launch_note = f"requested headful, fallback to headless: {launch_err}"
            self._context = await self._browser.new_context(viewport={"width": 1280, "height": 800})
            self._page = await self._context.new_page()
            self._headless = headless
            self._requested_headless = requested_headless
        elif self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        if not self._headless:
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
        self._bind_page_diagnostics(self._page)
        return self._page

    async def shutdown(self):
        """Clean up browser resources."""
        try:
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
                self._failed_requests.append({
                    "url": url[:300],
                    "error": error_text[:300],
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
        script = """
() => {
  const normalize = (value, maxLen = 160) => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, maxLen);
  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 1 && rect.height > 1;
  };
  const cssPath = (el) => {
    if (!el) return '';
    if (el.id) return `#${el.id}`;
    const dataTestId = el.getAttribute('data-testid');
    if (dataTestId) return `[data-testid="${dataTestId}"]`;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 4) {
      let part = node.tagName.toLowerCase();
      const nameAttr = node.getAttribute('name');
      if (nameAttr) {
        part += `[name="${nameAttr}"]`;
        parts.unshift(part);
        break;
      }
      const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((child) => child.tagName === node.tagName) : [];
      if (siblings.length > 1) {
        part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };
  const interactive = Array.from(document.querySelectorAll('button, a, input, textarea, select, summary, [role="button"], [role="link"], [role="tab"], [onclick], [data-testid], [tabindex]'))
    .filter(isVisible)
    .slice(0, LIMIT)
    .map((el, idx) => ({
      ref: `ref-${idx + 1}`,
      tag: el.tagName.toLowerCase(),
      role: normalize(el.getAttribute('role')),
      text: normalize(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title')),
      id: normalize(el.id),
      name: normalize(el.getAttribute('name')),
      label: normalize(el.getAttribute('aria-label')),
      placeholder: normalize(el.getAttribute('placeholder')),
      selector: normalize(cssPath(el), 220),
    }));
  const bodyText = normalize(document.body ? document.body.innerText : '', 2000);
  return {
    title: document.title || '',
    url: location.href,
    body_text: bodyText,
    interactive,
    counts: {
      buttons: document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]').length,
      links: document.querySelectorAll('a, [role="link"]').length,
      forms: document.forms.length,
      inputs: document.querySelectorAll('input, textarea, select').length,
      canvas: document.querySelectorAll('canvas').length,
    }
  };
}
        """.replace("LIMIT", str(max(1, min(limit, 80))))
        try:
            snapshot = await page.evaluate(script)
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
        diagnostics = self._diagnostics_summary()
        data.update(diagnostics)
        self._action_log.append({
            "action": action,
            "url": page.url,
            "state_hash": data.get("state_hash", ""),
        })
        self._action_log = self._action_log[-20:]
        return PluginResult(success=True, data=data, artifacts=artifacts)

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            action = str(params.get("action", "navigate") or "navigate").strip().lower()
            page = await self._ensure_browser(context=context)
            url = str(params.get("url", "") or "").strip()
            if url and url.strip():
                if action == "navigate":
                    self._clear_diagnostics()
                await page.goto(url.strip(), wait_until="domcontentloaded")
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
                    return PluginResult(success=False, error="click action requires a ref/selector/text/role/label/placeholder that exists")
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
                await page.mouse.wheel(0, delta)
                await page.wait_for_timeout(600)
                return await self._finalize_browser_result(
                    page,
                    action="scroll",
                    base_data={"direction": direction, "amount": amount},
                    include_screenshot=True,
                    full_page=False,
                    include_snapshot=bool(params.get("include_snapshot", False)),
                )

            if action == "press":
                key = str(params.get("key", "") or "").strip()
                if not key:
                    return PluginResult(success=False, error="press action requires a key")
                locator, target = await self._resolve_locator(page, params)
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

            return PluginResult(success=False, error=f"Unknown action: {action}")
        except Exception as e:
            return PluginResult(success=False, error=str(e))

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["navigate", "observe", "act", "snapshot", "click", "fill", "extract", "scroll", "press", "press_sequence", "wait_for"]},
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
                "limit": {"type": "integer", "description": "Snapshot element limit"},
                "mode": {"type": "string", "description": "For extract/act helpers: auto, structured, summary, click, fill, wait"},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Optional high-level fields to extract from the page"},
                "include_snapshot": {"type": "boolean", "description": "Include structured page snapshot in action result"},
                "include_screenshot": {"type": "boolean", "description": "Capture screenshot for non-visual actions like fill/wait_for"},
                "screenshot": {"type": "boolean", "description": "Capture screenshot for snapshot action"},
                "full_page": {"type": "boolean", "description": "Capture a full-page screenshot when supported"}
            },
            "required": ["action"]
        }


# ─────────────────────────────────────────────
# 3. File Operations Plugin
# ─────────────────────────────────────────────
class FileOpsPlugin(Plugin):
    name = "file_ops"
    display_name = "File Ops"
    description = "Read, write, list, and manage local files"
    icon = "fa-file"
    security_level = SecurityLevel.L2

    def _is_allowed_path(self, path: str, allowed_dirs) -> bool:
        if not path:
            return False
        candidate = Path(path).expanduser()
        # For existing paths, resolve directly; for new files, resolve parent
        if candidate.exists():
            resolved = candidate.resolve()
        else:
            try:
                resolved = candidate.parent.resolve() / candidate.name
            except Exception:
                resolved = candidate.absolute()

        roots = []
        for allowed in allowed_dirs or []:
            try:
                roots.append(Path(allowed).expanduser().resolve())
            except Exception:
                continue

        return any(resolved == root or root in resolved.parents or resolved.parent == root or root in resolved.parent.parents for root in roots)

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            action = params.get("action", "read")
            path = params.get("path", "")

            # Security check: validate against allowed directories
            allowed_dirs = context.get("allowed_dirs", ["/tmp"]) if context else ["/tmp"]
            if not self._is_allowed_path(path, allowed_dirs):
                return PluginResult(success=False, error=f"Path not allowed by security policy: {path}")

            if action == "read":
                if not os.path.exists(path):
                    return PluginResult(success=False, error=f"File not found: {path}")
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(500_000)  # Limit to 500KB
                return PluginResult(success=True, data={
                    "path": path, "content": content, "size": os.path.getsize(path)
                })
            elif action == "write":
                content = params.get("content", "")
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
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
            elif action == "delete":
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
                "action": {"type": "string", "enum": ["read", "write", "list", "delete"]},
                "path": {"type": "string", "description": "File or directory path"},
                "content": {"type": "string", "description": "Content to write (for write action)"}
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
# 6. Computer Use Plugin (GPT-5.4 CUA)
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

            client = AsyncOpenAI(api_key=api_key)

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
# 7. UI Control Plugin
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
                os.system(f'osascript -e \'tell application "{app_name}" to activate\'')
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


# ─────────────────────────────────────────────
# Auto-register all plugins
# ─────────────────────────────────────────────
def register_all():
    """Register all built-in plugins."""
    for PluginClass in [ScreenshotPlugin, BrowserPlugin, FileOpsPlugin, ComfyUIPlugin,
                        ShellPlugin, GitPlugin, ComputerUsePlugin, UIControlPlugin]:
        PluginRegistry.register(PluginClass())
    logger.info(f"Registered {len(PluginRegistry.get_all())} plugins")
