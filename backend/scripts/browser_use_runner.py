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
    # v5.8.1: default 10 → 6. Most QA flows (click play, wait, observe state)
    # finish in 4-5 steps; capping at 6 keeps the loop tight. Callers can
    # still request up to 40 explicitly if needed.
    max_steps = max(2, min(int(payload.get("max_steps", 6) or 6), 40))
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
    # v5.8.2: pass through additional browser-use features previously dropped.
    allowed_domains = payload.get("allowed_domains")
    if allowed_domains and isinstance(allowed_domains, list):
        allowed_domains = [str(d).strip() for d in allowed_domains if str(d).strip()]
    else:
        allowed_domains = None
    viewport = payload.get("viewport")
    if not (isinstance(viewport, dict) and viewport.get("width") and viewport.get("height")):
        viewport = None
    generate_gif = payload.get("generate_gif")
    if isinstance(generate_gif, bool) and generate_gif:
        generate_gif_path = str(artifact_dir / "session_flow.gif")
    elif isinstance(generate_gif, str) and generate_gif.strip():
        generate_gif_path = generate_gif.strip()
    else:
        generate_gif_path = None
    save_conversation_path = str(payload.get("save_conversation_path") or (artifact_dir / "conversation.log"))
    max_actions_per_step = int(payload.get("max_actions_per_step", 4) or 4)

    # v6.4.8: CDP attach is OPT-IN ONLY.
    # Rationale: the previous auto-probe (19222/9222) made the AI agent hijack
    # the user's Evermind embedded Chromium session — every AI navigate/click
    # yanked the user's visible tab away. The AI should always drive its own
    # browser context. Callers that genuinely need CDP (e.g. testing an already-
    # open page) must pass payload["cdp_url"] or set
    # EVERMIND_BROWSER_ATTACH_CDP=1 together with EVERMIND_BROWSER_CDP_URL.
    cdp_url_preferred = str(payload.get("cdp_url") or "").strip()
    if not cdp_url_preferred:
        attach_flag = str(os.environ.get("EVERMIND_BROWSER_ATTACH_CDP", "0")).strip().lower() in {"1", "true", "yes", "on"}
        env_url = str(os.environ.get("EVERMIND_BROWSER_CDP_URL", "")).strip()
        if attach_flag and env_url:
            cdp_url_preferred = env_url
    browser_attached_via_cdp = False

    try:
        browser = None
        if cdp_url_preferred:
            # Try CDP attach first. Different browser-use versions accept
            # cdp_url in different places; try a few shapes.
            for attempt in ("config_cdp", "direct_cdp", "browser_context_cdp"):
                try:
                    if attempt == "config_cdp":
                        browser = Browser(config=BrowserConfig(cdp_url=cdp_url_preferred, headless=headless))
                    elif attempt == "direct_cdp":
                        browser = Browser(cdp_url=cdp_url_preferred)
                    elif attempt == "browser_context_cdp" and BrowserContextConfig is not None:
                        browser = Browser(
                            config=BrowserConfig(headless=headless),
                            new_context_config=BrowserContextConfig(cdp_url=cdp_url_preferred),
                        )
                    if browser is not None:
                        browser_attached_via_cdp = True
                        break
                except Exception:
                    browser = None
                    continue

        if browser is None:
            # v6.4.8: if CDP attach was requested and failed,
            # fall back to independent Playwright Chromium. Previously we raised
            # RuntimeError here to avoid "second window popping up"; that actually
            # made every CDP miss turn into a node failure. Independent Chromium
            # is the correct default — it keeps the AI session strictly separate
            # from the user's browser.
            browser_kwargs: Dict[str, Any] = {"headless": headless}
            if allowed_domains:
                browser_kwargs["allowed_domains"] = allowed_domains
            if viewport:
                browser_kwargs["viewport"] = {"width": int(viewport["width"]), "height": int(viewport["height"])}
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
                safe_kwargs = {k: v for k, v in browser_kwargs.items() if k in {"headless", "new_context_config"}}
                try:
                    browser = Browser(config=BrowserConfig(**safe_kwargs))
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
        if generate_gif_path:
            agent_kwargs["generate_gif"] = generate_gif_path
        if save_conversation_path:
            agent_kwargs["save_conversation_path"] = save_conversation_path
        if max_actions_per_step:
            agent_kwargs["max_actions_per_step"] = max_actions_per_step
        # Agent() may not accept all kwargs on older browser-use versions; retry
        # with only the universally-supported subset if the full set fails.
        try:
            agent = Agent(**agent_kwargs)
        except TypeError:
            core = {k: agent_kwargs[k] for k in ("task", "llm", "browser", "use_vision") if k in agent_kwargs}
            agent = Agent(**core)

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
    # v5.8.1: cap history to the 12 most recent steps (was 80). 80 steps × ~10KB
    # per-state = 800KB+ replayed back to the AI — bloats every downstream
    # prompt. 12 is enough to show what the browser did without dumping the
    # entire tape. Errors still capped at 40 since they're small.
    if history_items:
        data["history_items"] = history_items[-12:]
    if action_names:
        data["action_names"] = [str(name)[:80] for name in action_names[-12:]]
    if urls:
        data["urls"] = [str(item)[:500] for item in urls[-12:] if str(item).strip()]
    if screenshot_paths:
        data["screenshot_paths"] = [str(item)[:500] for item in screenshot_paths[-12:] if str(item).strip()]
    if errors:
        data["errors"] = [str(item)[:300] for item in errors[-40:] if str(item).strip()]
    data["is_done"] = is_done
    data["is_successful"] = is_successful
    # v5.8.2: surface the CDP-attach decision so the UI / downstream agents
    # can show "AI is driving Evermind's embedded Chromium" vs "AI spawned
    # its own Playwright Chromium".
    data["cdp_attached"] = bool(browser_attached_via_cdp)
    data["cdp_url"] = cdp_url_preferred if browser_attached_via_cdp else ""

    _emit({"success": True, "data": data, "artifacts": artifacts})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
