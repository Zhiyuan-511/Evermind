#!/usr/bin/env python3
"""
Sidecar runner for the optional browser-use integration.

This script is intentionally isolated so Evermind can keep browser-use out of the
main backend dependency graph. The caller may point it at a separate venv via
EVERMIND_BROWSER_USE_PYTHON / browser_use_python.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _read_payload() -> Dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def _call_maybe(obj: Any, attr_name: str, default: Any = None) -> Any:
    try:
        attr = getattr(obj, attr_name, None)
        if attr is None:
            return default
        return attr() if callable(attr) else attr
    except Exception:
        return default


def _normalize_action_name(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    lowered = lowered.replace("action", "")
    parts = [part for part in lowered.replace("-", "_").split("_") if part]
    return "_".join(parts)


def _action_names_from_payload(payload: Any) -> List[str]:
    names: List[str] = []

    def _append(value: Any) -> None:
        name = _normalize_action_name(value)
        if name and name not in names:
            names.append(name)

    if isinstance(payload, list):
        for item in payload:
            names.extend([name for name in _action_names_from_payload(item) if name not in names])
        return names
    if isinstance(payload, dict):
        explicit_type = payload.get("type") or payload.get("action_type") or payload.get("name")
        if explicit_type:
            _append(explicit_type)
        for key, value in payload.items():
            if key in {"type", "action_type", "name"}:
                continue
            if isinstance(value, dict):
                _append(key)
            elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                _append(key)
        if not names and len(payload) == 1:
            _append(next(iter(payload.keys())))
        return names
    _append(payload)
    return names


def _flatten_errors(payload: Any) -> List[str]:
    errors: List[str] = []
    if isinstance(payload, str):
        text = payload.strip()
        if text:
            errors.append(text[:300])
        return errors
    if isinstance(payload, dict):
        for key in ("error", "message", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                errors.append(value.strip()[:300])
        return errors
    if isinstance(payload, list):
        for item in payload:
            for entry in _flatten_errors(item):
                if entry not in errors:
                    errors.append(entry)
        return errors
    return errors


def _extract_history_items(result: Any) -> List[Dict[str, Any]]:
    history_dump = None
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        for kwargs in ({"mode": "python"}, {}):
            try:
                history_dump = model_dump(**kwargs)
                break
            except TypeError:
                continue
            except Exception:
                break

    if isinstance(history_dump, list):
        raw_items = history_dump
    elif isinstance(history_dump, dict):
        raw_items = history_dump.get("history") or history_dump.get("steps") or history_dump.get("items") or []
    else:
        raw_items = []

    history_items: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        state = item.get("state") if isinstance(item.get("state"), dict) else {}
        model_output = item.get("model_output") if isinstance(item.get("model_output"), dict) else {}
        action_payload = model_output.get("action")
        action_names = _action_names_from_payload(action_payload)
        step_errors = _flatten_errors(item.get("result"))
        if not step_errors:
            step_errors = _flatten_errors(item.get("error"))
        history_items.append({
            "step": idx + 1,
            "action_names": action_names,
            "url": str(state.get("url") or "").strip(),
            "screenshot_path": str(state.get("screenshot_path") or "").strip(),
            "interacted_element": state.get("interacted_element"),
            "errors": step_errors,
        })

    if history_items:
        return history_items

    action_names = _call_maybe(result, "action_names", []) or []
    urls = _call_maybe(result, "urls", []) or []
    screenshot_paths = list(getattr(result, "screenshot_paths", []) or [])
    for idx, name in enumerate(action_names):
        history_items.append({
            "step": idx + 1,
            "action_names": [_normalize_action_name(name)],
            "url": str(urls[idx] if idx < len(urls) else (urls[-1] if urls else "")).strip(),
            "screenshot_path": str(
                screenshot_paths[idx]
                if idx < len(screenshot_paths)
                else (screenshot_paths[-1] if screenshot_paths else "")
            ).strip(),
            "interacted_element": None,
            "errors": [],
        })
    return history_items


async def _main() -> int:
    payload = _read_payload()
    if not str(os.getenv("BROWSER_USE_CONFIG_DIR", "") or "").strip():
        os.environ["BROWSER_USE_CONFIG_DIR"] = str(Path.home() / ".evermind" / "browser_use_config")
    Path(os.environ["BROWSER_USE_CONFIG_DIR"]).expanduser().mkdir(parents=True, exist_ok=True)
    task = str(payload.get("task") or payload.get("instruction") or "").strip()
    url = str(payload.get("url") or "").strip()
    model = str(payload.get("model") or "gpt-4o").strip()
    max_steps = max(2, min(int(payload.get("max_steps", 10) or 10), 40))
    headless = _coerce_bool(payload.get("headless"), default=True)
    use_vision = _coerce_bool(payload.get("use_vision"), default=True)
    artifact_dir = Path(str(payload.get("artifact_dir") or "/tmp/browser_use_artifacts")).expanduser()
    recordings_dir = Path(str(payload.get("recordings_dir") or artifact_dir / "recordings")).expanduser()
    screenshots_dir = Path(str(payload.get("screenshots_dir") or artifact_dir / "screenshots")).expanduser()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    recordings_dir.mkdir(parents=True, exist_ok=True)
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    if not task and not url:
        _emit({"success": False, "error": "browser_use_runner requires task or url"})
        return 1

    openai_key = str(os.getenv("OPENAI_API_KEY", "") or "").strip()
    if not openai_key:
        _emit({"success": False, "error": "OPENAI_API_KEY is required for browser_use_runner"})
        return 1

    try:
        from browser_use import Agent, Browser, BrowserConfig, ChatOpenAI  # type: ignore
    except Exception:
        try:
            from browser_use import Agent, Browser, BrowserConfig  # type: ignore
            from langchain_openai import ChatOpenAI  # type: ignore
        except Exception as exc:
            _emit({"success": False, "error": f"browser_use import failed: {str(exc)[:240]}"})
            return 1

    BrowserContextConfig = None
    for mod_name in ("browser_use.browser.context", "browser_use"):
        try:
            module = __import__(mod_name, fromlist=["BrowserContextConfig"])
            BrowserContextConfig = getattr(module, "BrowserContextConfig", None)
            if BrowserContextConfig is not None:
                break
        except Exception:
            continue

    llm_kwargs: Dict[str, Any] = {"model": model, "api_key": openai_key}
    base_url = str(os.getenv("OPENAI_BASE_URL", "") or "").strip()
    if base_url:
        llm_kwargs["base_url"] = base_url
    llm = ChatOpenAI(**llm_kwargs)

    browser = None
    try:
        browser_kwargs: Dict[str, Any] = {"headless": headless}
        if BrowserContextConfig is not None:
            try:
                browser_kwargs["new_context_config"] = BrowserContextConfig(
                    save_recording_path=str(recordings_dir)
                )
            except TypeError:
                try:
                    browser_kwargs["new_context_config"] = BrowserContextConfig(
                        recording_path=str(recordings_dir)
                    )
                except Exception:
                    pass
        try:
            browser = Browser(config=BrowserConfig(**browser_kwargs))
        except Exception:
            browser = Browser()

        composed_task = task or "Inspect the page and interact with the main controls."
        if url:
            composed_task = (
                f"Open {url} first. Then {composed_task} "
                "Focus on meaningful interaction rather than passive observation. "
                "If this is a game, click the visible play/start control and test movement / action input."
            )

        agent_kwargs: Dict[str, Any] = {
            "task": composed_task,
            "llm": llm,
            "browser": browser,
        }
        if use_vision:
            agent_kwargs["use_vision"] = True
        agent = Agent(**agent_kwargs)

        try:
            result = await agent.run(max_steps=max_steps)
        except TypeError:
            result = await agent.run()
    except Exception as exc:
        if browser is not None:
            try:
                maybe_close = getattr(browser, "close", None)
                if callable(maybe_close):
                    maybe_result = maybe_close()
                    if asyncio.iscoroutine(maybe_result):
                        await maybe_result
            except Exception:
                pass
        _emit({"success": False, "error": f"browser_use execution failed: {str(exc)[:320]}"})
        return 1

    try:
        maybe_close = getattr(browser, "close", None)
        if callable(maybe_close):
            maybe_result = maybe_close()
            if asyncio.iscoroutine(maybe_result):
                await maybe_result
    except Exception:
        pass

    result_text = ""
    for attr_name in ("final_result", "result", "summary"):
        try:
            attr = getattr(result, attr_name, None)
            candidate = attr() if callable(attr) else attr
            if candidate:
                result_text = str(candidate)
                break
        except Exception:
            continue
    if not result_text:
        result_text = str(result)

    artifacts: List[Dict[str, Any]] = []
    media_exts = {".png", ".jpg", ".jpeg", ".gif", ".webm", ".mp4", ".mov", ".zip"}
    for folder in (screenshots_dir, recordings_dir, artifact_dir):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in media_exts:
                continue
            artifact_type = "video" if path.suffix.lower() in {".webm", ".mp4", ".mov"} else "image"
            if path.suffix.lower() == ".zip":
                artifact_type = "trace"
            entry = {"type": artifact_type, "path": str(path)}
            if entry not in artifacts:
                artifacts.append(entry)

    data: Dict[str, Any] = {
        "task": composed_task,
        "output": result_text[:4000],
        "final_url": url,
        "steps": max_steps,
        "browser_mode": "headless" if headless else "headful",
        "recordings_dir": str(recordings_dir),
        "screenshots_dir": str(screenshots_dir),
        "artifact_count": len(artifacts),
    }
    if artifacts:
        first_video = next((item["path"] for item in artifacts if item.get("type") == "video"), "")
        first_image = next((item["path"] for item in artifacts if item.get("type") == "image"), "")
        if first_video:
            data["recording_path"] = first_video
        if first_image:
            data["capture_path"] = first_image

    history_items = _extract_history_items(result)
    action_names = _call_maybe(result, "action_names", []) or []
    urls = _call_maybe(result, "urls", []) or []
    screenshot_paths = list(getattr(result, "screenshot_paths", []) or [])
    errors = _call_maybe(result, "errors", []) or []
    is_done = bool(_call_maybe(result, "is_done", False))
    is_successful = bool(_call_maybe(result, "is_successful", False))
    if history_items:
        data["history_items"] = history_items[:80]
    if action_names:
        data["action_names"] = [str(name)[:80] for name in action_names[:80]]
    if urls:
        data["urls"] = [str(item)[:500] for item in urls[:80] if str(item).strip()]
    if screenshot_paths:
        data["screenshot_paths"] = [str(item)[:500] for item in screenshot_paths[:80] if str(item).strip()]
    if errors:
        data["errors"] = [str(item)[:300] for item in errors[:40] if str(item).strip()]
    data["is_done"] = is_done
    data["is_successful"] = is_successful

    _emit({"success": True, "data": data, "artifacts": artifacts})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
