"""
Evermind Backend — Plugin Implementations
Seven core plugins: screenshot, browser, file_ops, shell, git, computer_use, ui_control
"""

import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

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
    description = "Open web pages, click elements, fill forms, and extract content from a persistent browser session"
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

    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        try:
            action = params.get("action", "navigate")
            page = await self._ensure_browser(context=context)
            browser_mode = "headless" if self._headless else "headful"
            requested_mode = "headless" if self._requested_headless else "headful"
            mode_data = {
                "browser_mode": browser_mode,
                "requested_mode": requested_mode,
                "launch_note": self._launch_note,
            }

            url = params.get("url")
            if url and url.strip():
                await page.goto(url.strip(), wait_until="domcontentloaded")
            elif url is not None:  # url was provided but empty/whitespace
                url = None  # treat as no URL provided

            if action == "navigate":
                if not url:
                    return PluginResult(success=False, error="navigate action requires a url")
                content = await page.content()
                title = await page.title()
                screenshot_bytes = await page.screenshot(full_page=params.get("full_page", False))
                b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                return PluginResult(
                    success=True,
                    data={"title": title, "url": page.url, "content_length": len(content), **mode_data},
                    artifacts=[{"type": "image", "base64": b64}]
                )

            if action == "click":
                selector = params.get("selector")
                if not selector:
                    return PluginResult(success=False, error="click action requires a selector")
                await page.click(selector)
                await page.wait_for_timeout(params.get("wait_ms", 800))
                screenshot_bytes = await page.screenshot(full_page=params.get("full_page", False))
                b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                return PluginResult(
                    success=True,
                    data={"action": "click", "selector": selector, "url": page.url, **mode_data},
                    artifacts=[{"type": "image", "base64": b64}]
                )

            if action == "fill":
                selector = params.get("selector")
                value = params.get("value", "")
                if not selector:
                    return PluginResult(success=False, error="fill action requires a selector")
                await page.fill(selector, value)
                if params.get("submit"):
                    await page.press(selector, "Enter")
                return PluginResult(success=True, data={"filled": selector, "url": page.url, **mode_data})

            if action == "extract":
                selector = params.get("selector", "body")
                text = await page.text_content(selector)
                return PluginResult(success=True, data={"text": (text or "")[:5000], "url": page.url, "selector": selector, **mode_data})

            if action == "scroll":
                direction = params.get("direction", "down")
                amount = int(params.get("amount", 500))
                delta = amount if direction == "down" else -amount
                await page.mouse.wheel(0, delta)
                await page.wait_for_timeout(600)
                screenshot_bytes = await page.screenshot(full_page=False)
                b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                return PluginResult(
                    success=True,
                    data={"action": "scroll", "direction": direction, "amount": amount, "url": page.url, **mode_data},
                    artifacts=[{"type": "image", "base64": b64}]
                )

            return PluginResult(success=False, error=f"Unknown action: {action}")
        except Exception as e:
            return PluginResult(success=False, error=str(e))

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["navigate", "click", "fill", "extract", "scroll"]},
                "url": {"type": "string", "description": "URL to navigate to before performing the action"},
                "selector": {"type": "string", "description": "CSS selector for click/fill/extract"},
                "value": {"type": "string", "description": "Value for fill action"},
                "submit": {"type": "boolean", "description": "Press Enter after filling the field"},
                "wait_ms": {"type": "integer", "description": "Optional wait after click"},
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
# 4. Shell Plugin
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
    for PluginClass in [ScreenshotPlugin, BrowserPlugin, FileOpsPlugin,
                        ShellPlugin, GitPlugin, ComputerUsePlugin, UIControlPlugin]:
        PluginRegistry.register(PluginClass())
    logger.info(f"Registered {len(PluginRegistry.get_all())} plugins")
