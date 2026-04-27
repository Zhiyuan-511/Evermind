#!/usr/bin/env python3
"""Multi-difficulty smoke harness.

Runs three end-to-end Evermind pipelines back-to-back:
    simple    → 3 nodes  (builder → deployer → tester)
    standard  → ~5-7 nodes  (planner + analyst + builder + reviewer + deployer)
    pro       → 11-12 nodes (full game DAG with parallel integrator builders)

Each run uses a fresh session_id so cross-run context never pollutes. External
AI agents (including Claude Code) can invoke this script to exercise the
whole stack without a human touching the UI.

Usage:
    python3 backend/scripts/multi_difficulty_smoke.py
    python3 backend/scripts/multi_difficulty_smoke.py --only simple
    python3 backend/scripts/multi_difficulty_smoke.py --model aigate-deepseek-v3.2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

try:
    import websockets
except ImportError:
    raise SystemExit("websockets not installed — run: pip install websockets")


DEFAULT_WS = "ws://127.0.0.1:8765/ws"
STORE_DIR = Path.home() / ".evermind"

# One goal per difficulty. Each targets the complexity that difficulty expects.
GOALS: Dict[str, str] = {
    "simple": "创建一个极简的个人主页 index.html,纯白底,居中标题'Hello',一行副标题'Welcome'。",
    "standard": "创建一个小型咖啡馆的官方网站,三个页面(首页/菜单/关于),暖色调,响应式布局,简洁的菜单卡片。",
    "pro": "创建一个 3D 第三人称射击小游戏(WASD+鼠标视角+左键射击),单一关卡,3 种敌人,起始菜单,HUD 显示血量和弹药。保存为 index.html。",
}

# Node-count expectation per difficulty. Used for ✓/✗ reporting.
EXPECTED_NODE_COUNT: Dict[str, tuple[int, int]] = {
    "simple": (3, 6),
    "standard": (4, 8),
    "pro": (10, 14),
}


async def run_one(difficulty: str, goal: str, model: str, ws_url: str,
                  timeout_s: int) -> Dict[str, Any]:
    session_id = f"smoke-{difficulty}-{uuid.uuid4().hex[:8]}"
    started = time.time()
    async with websockets.connect(ws_url, open_timeout=15) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("type") != "connected":
            raise RuntimeError(f"unexpected hello: {hello}")

        payload = {
            "type": "run_goal",
            "goal": goal,
            "difficulty": difficulty,
            "model": model,
            "runtime": "local",
            "chat_history": [],
            "session_id": session_id,
        }
        await ws.send(json.dumps(payload, ensure_ascii=False))
        print(f"[{difficulty}] sent run_goal — session={session_id} model={model}")

        # Wait for ack
        ack = None
        deadline = time.time() + 30
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            msg = json.loads(raw)
            if msg.get("type") == "run_goal_ack":
                ack = msg.get("payload") or {}
                break
        if not ack:
            return {"ok": False, "reason": "no ack", "difficulty": difficulty}

        run_id = str(ack.get("runId") or "")
        nodes_listed = ack.get("nodeExecutions") or []
        node_keys = [n.get("node_key") for n in nodes_listed]
        print(f"[{difficulty}] run_id={run_id} nodes={len(node_keys)}: {node_keys}")

    # Poll runs.json / node_executions.json until terminal state
    terminal = {"done", "failed", "cancelled", "waiting_review", "waiting_selfcheck"}
    poll_deadline = time.time() + timeout_s
    last_phase = None
    while time.time() < poll_deadline:
        try:
            runs = json.load(open(STORE_DIR / "runs.json"))
            r = next((x for x in runs if x.get("id") == run_id), None)
            if r:
                state = r.get("status") or r.get("state")
                if state in terminal:
                    break
                if state != last_phase:
                    print(f"[{difficulty}] status={state}")
                    last_phase = state
        except Exception:
            pass
        await asyncio.sleep(4)

    # Final report
    nes = json.load(open(STORE_DIR / "node_executions.json"))
    run_nes = [ne for ne in nes if ne.get("run_id") == run_id]
    passed = sum(1 for ne in run_nes if ne.get("status") == "passed")
    failed = sum(1 for ne in run_nes if ne.get("status") == "failed")
    total_code_lines = sum(int(ne.get("code_lines") or 0) for ne in run_nes)
    total_all_lines = sum(int(ne.get("total_lines") or 0) for ne in run_nes)
    status = (r or {}).get("status") if r else "unknown"
    duration = time.time() - started

    return {
        "ok": (status in {"done", "waiting_review", "waiting_selfcheck"}) and failed == 0,
        "difficulty": difficulty,
        "run_id": run_id,
        "status": status,
        "node_count": len(run_nes),
        "passed_nodes": passed,
        "failed_nodes": failed,
        "code_lines": total_code_lines,
        "total_lines": total_all_lines,
        "duration_s": int(duration),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["simple", "standard", "pro"], default=None,
                    help="Run a single difficulty instead of all 3")
    ap.add_argument("--model", default="aigate-deepseek-v3.2",
                    help="Router model (nodes may override)")
    ap.add_argument("--ws", default=DEFAULT_WS)
    ap.add_argument("--timeout", type=int, default=900, help="Per-run timeout (s)")
    args = ap.parse_args()

    targets = [args.only] if args.only else ["simple", "standard", "pro"]
    results: List[Dict[str, Any]] = []
    for d in targets:
        print(f"\n{'='*60}\n{d.upper()} — {GOALS[d][:70]}...\n{'='*60}")
        try:
            res = await run_one(d, GOALS[d], args.model, args.ws, args.timeout)
        except Exception as exc:
            res = {"ok": False, "difficulty": d, "reason": str(exc)[:200]}
        results.append(res)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        # Cool-down between runs so provider rate limits reset
        await asyncio.sleep(5)

    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    for r in results:
        ok = "✓" if r.get("ok") else "✗"
        line = f"{ok} {r['difficulty']:10s}"
        if r.get("run_id"):
            line += f" run_id={r['run_id']}"
            line += f" nodes={r.get('passed_nodes','?')}/{r.get('node_count','?')} passed"
            line += f" code={r.get('code_lines',0)}L files={r.get('total_lines',0)}L"
            line += f" {r.get('duration_s','?')}s"
        else:
            line += f" — {r.get('reason','failed')}"
        print(line)

    all_ok = all(r.get("ok") for r in results)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
