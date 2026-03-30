#!/usr/bin/env python3
"""
Live LLM + real browser smoke for Evermind reviewer/tester flows.

This script uses the configured local Evermind API key (e.g. ~/.evermind/config.json)
and runs a real model through the BrowserPlugin against local temporary fixtures.

It is intentionally small and opinionated:
- website -> reviewer flow
- game -> tester flow
- dashboard -> reviewer flow
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_bridge import AIBridge
from plugins.implementations import BrowserPlugin
from settings import apply_api_keys, load_settings
from scripts.browser_quality_smoke import DASHBOARD_HTML, GAME_HTML, WEBSITE_HTML


CASE_DEFS: Dict[str, Dict[str, Any]] = {
    "website": {
        "role": "reviewer",
        "filename": "website.html",
        "html": WEBSITE_HTML,
        "required_actions": {"snapshot", "click"},
        "requires_post_verify": True,
        "prompt": (
            "You are a strict reviewer. Use the browser tool to verify the product website at the URL in the user message.\n"
            "The very first observation step MUST be browser.snapshot. Do not click before snapshot.\n"
            "Required steps:\n"
            "1. navigate to the URL\n"
            "2. snapshot the page as the first inspection action\n"
            "3. click the 'Get Started' button\n"
            "4. after clicking, you MUST call wait_for on 'Welcome aboard' or take a second snapshot to verify the changed state\n"
            "5. output compact JSON with verdict, evidence, and issues\n"
            "Reject if any required interaction cannot be verified."
        ),
    },
    "game": {
        "role": "tester",
        "filename": "game.html",
        "html": GAME_HTML,
        "required_actions": {"snapshot", "click", "press_sequence"},
        "requires_post_verify": False,
        "prompt": (
            "You are a strict gameplay tester. Use the browser tool to test the browser game at the URL in the user message.\n"
            "The very first observation step MUST be browser.snapshot. Do not click before snapshot.\n"
            "Required steps:\n"
            "1. navigate to the URL\n"
            "2. snapshot the start screen as the first inspection action\n"
            "3. click 'Start Game'\n"
            "4. use press_sequence with gameplay keys such as ArrowRight, ArrowLeft, ArrowDown, and Space\n"
            "5. verify the visible state changes and the score is no longer 0\n"
            "6. output compact JSON with verdict, evidence, and issues\n"
            "Reject if the game cannot actually be played."
        ),
    },
    "dashboard": {
        "role": "reviewer",
        "filename": "dashboard.html",
        "html": DASHBOARD_HTML,
        "required_actions": {"snapshot", "click"},
        "requires_post_verify": True,
        "prompt": (
            "You are a strict reviewer. Use the browser tool to verify the dashboard at the URL in the user message.\n"
            "The very first observation step MUST be browser.snapshot. Do not click before snapshot.\n"
            "Required steps:\n"
            "1. navigate to the URL\n"
            "2. snapshot the controls as the first inspection action\n"
            "3. click 'Monthly'\n"
            "4. after clicking, you MUST call wait_for on 'Monthly trend accelerating' or take a second snapshot to verify the changed state\n"
            "5. click 'North America'\n"
            "6. after clicking, you MUST call wait_for on 'Region: Europe' or take a second snapshot to verify the changed state\n"
            "7. output compact JSON with verdict, evidence, and issues\n"
            "Reject if the controls do not visibly change the dashboard."
        ),
    },
}


async def run_case(case_name: str, model: str) -> None:
    settings = load_settings()
    apply_api_keys(settings)
    if not (settings.get("api_keys", {}) or {}).get("kimi"):
        raise RuntimeError("No kimi API key found in ~/.evermind/config.json")

    case = CASE_DEFS[case_name]
    bridge = AIBridge(config={})
    plugin = BrowserPlugin()
    browser_actions: List[Dict[str, Any]] = []
    progress_log: List[Dict[str, Any]] = []

    try:
        with tempfile.TemporaryDirectory(prefix=f"evermind_live_agent_{case_name}_") as tmpdir:
            tmp_path = Path(tmpdir)
            html_path = tmp_path / case["filename"]
            html_path.write_text(case["html"], encoding="utf-8")
            file_url = html_path.resolve().as_uri()

            async def on_progress(data: Dict[str, Any]) -> None:
                progress_log.append(dict(data))
                stage = str(data.get("stage") or "").strip().lower()
                if stage == "browser_action":
                    browser_actions.append(dict(data))
                    action = data.get("action")
                    ok = data.get("ok")
                    target = data.get("target") or ""
                    changed = data.get("state_changed")
                    print(f"  browser_action action={action} ok={ok} target={target} state_changed={changed}")
                elif stage == "qa_followup":
                    print(f"  qa_followup {data.get('message')}")

            node = {
                "id": f"live_{case_name}",
                "type": case["role"],
                "name": f"Live {case['role'].title()}",
                "prompt": case["prompt"],
                "model": model,
            }

            print(f"[{case_name}] model={model} url={file_url}")
            result = await bridge.execute(
                node=node,
                plugins=[plugin],
                input_data=f"Target URL: {file_url}",
                model=model,
                on_progress=on_progress,
            )

            if not result.get("success"):
                raise RuntimeError(f"LLM execution failed: {result.get('error')}")

            actions_seen = {
                str(item.get("action") or "").strip().lower()
                for item in browser_actions
                if item.get("ok")
            }
            missing = sorted(case["required_actions"] - actions_seen)
            if missing:
                raise RuntimeError(f"Required browser actions missing: {missing}")

            if case.get("requires_post_verify"):
                seen_interaction = False
                has_post_verify = False
                for item in browser_actions:
                    action = str(item.get("action") or "").strip().lower()
                    if action in {"click", "fill", "press", "press_sequence"}:
                        seen_interaction = True
                        continue
                    if seen_interaction and action in {"wait_for", "snapshot"}:
                        has_post_verify = True
                        break
                if not has_post_verify:
                    raise RuntimeError("Missing post-interaction verification action (wait_for/snapshot)")

            if case_name in {"website", "dashboard"} and not any(bool(item.get("state_changed", False)) for item in browser_actions):
                raise RuntimeError("No visible state change detected in browser actions")
            if case_name == "game" and not any(str(item.get("action") or "").strip().lower() == "press_sequence" for item in browser_actions):
                raise RuntimeError("Game case did not execute press_sequence")

            output = str(result.get("output") or "").strip()
            preview = output[:700]
            print(f"  output_preview={preview}")
            print(f"PASS [{case_name}] live agent browser smoke succeeded")
    finally:
        await plugin.shutdown()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["website", "game", "dashboard", "all"], default="website")
    parser.add_argument("--model", default="kimi-coding")
    args = parser.parse_args()

    cases = ["website", "game", "dashboard"] if args.case == "all" else [args.case]
    for case_name in cases:
        await run_case(case_name, args.model)


if __name__ == "__main__":
    asyncio.run(main())
