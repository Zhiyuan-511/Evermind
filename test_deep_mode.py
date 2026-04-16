#!/usr/bin/env python3
"""Evermind Deep Mode E2E Test — 贪吃蛇游戏"""
import asyncio
import json
import time
import sys

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

WS_URL = "ws://127.0.0.1:8765/ws"
GOAL = "做一个经典的贪吃蛇游戏，要求：1) 方向键控制蛇的移动 2) 吃到食物蛇身变长分数增加 3) 撞墙或撞自己游戏结束 4) 显示当前分数和最高分 5) 游戏结束后可以重新开始 6) 界面美观，有动画效果"
MODEL = "kimi-k2.5"
DIFFICULTY = "pro"

async def run_test():
    print(f"\n{'='*60}")
    print(f"Evermind Deep Mode Test")
    print(f"Goal: {GOAL[:60]}...")
    print(f"Model: {MODEL}  Difficulty: {DIFFICULTY}")
    print(f"{'='*60}\n")

    start_time = time.time()
    node_events = {}
    errors = []
    warnings = []
    progress_issues = []

    async with websockets.connect(WS_URL, max_size=10_000_000, ping_interval=30) as ws:
        # Wait for connected handshake
        handshake = json.loads(await ws.recv())
        print(f"[CONNECTED] version={handshake.get('version', '?')} plugins={len(handshake.get('plugins', []))}")

        # Send run_goal
        await ws.send(json.dumps({
            "type": "run_goal",
            "goal": GOAL,
            "model": MODEL,
            "difficulty": DIFFICULTY,
            "chat_history": [],
            "attachments": [],
            "session_id": f"test-deep-{int(time.time())}",
        }))
        print(f"[SENT] run_goal (deep mode)\n")

        # Monitor events
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=600)
                data = json.loads(raw)
                evt_type = data.get("type", "unknown")

                if evt_type == "plan_created":
                    subtasks = data.get("subtasks", [])
                    print(f"[PLAN] {len(subtasks)} nodes created:")
                    for st in subtasks:
                        node_id = st.get("id", "?")
                        agent = st.get("agent_type", "?")
                        deps = st.get("depends_on", [])
                        node_events[node_id] = {"agent": agent, "status": "pending", "progress_values": []}
                        print(f"  Node {node_id}: {agent} (depends: {deps})")
                    print()

                elif evt_type in ("node_started", "node_execution_started"):
                    nid = data.get("node_id") or data.get("subtask_id", "?")
                    agent = data.get("agent_type", "?")
                    if nid in node_events:
                        node_events[nid]["status"] = "running"
                    print(f"[START] Node {nid} ({agent})")

                elif evt_type in ("node_progress", "node_execution_progress"):
                    nid = data.get("node_id") or data.get("subtask_id", "?")
                    progress = data.get("progress", 0)
                    # Check progress clamp (V4.9.3 fix)
                    if isinstance(progress, (int, float)):
                        if progress < 0 or progress > 100:
                            issue = f"Node {nid}: invalid progress={progress}"
                            progress_issues.append(issue)
                            print(f"[BUG] {issue}")
                        if nid in node_events:
                            node_events[nid]["progress_values"].append(progress)

                elif evt_type in ("node_completed", "node_execution_completed"):
                    nid = data.get("node_id") or data.get("subtask_id", "?")
                    agent = data.get("agent_type", "?")
                    status = data.get("status", "completed")
                    duration = data.get("duration_seconds") or data.get("duration", "?")
                    if nid in node_events:
                        node_events[nid]["status"] = status
                    print(f"[DONE] Node {nid} ({agent}) — {status} ({duration}s)")

                elif evt_type in ("node_failed", "node_execution_failed"):
                    nid = data.get("node_id") or data.get("subtask_id", "?")
                    agent = data.get("agent_type", "?")
                    error = data.get("error", "?")[:200]
                    if nid in node_events:
                        node_events[nid]["status"] = "failed"
                    errors.append(f"Node {nid} ({agent}): {error}")
                    print(f"[FAIL] Node {nid} ({agent}): {error}")

                elif evt_type == "run_completed":
                    total_duration = time.time() - start_time
                    success = data.get("success", False)
                    print(f"\n{'='*60}")
                    print(f"[RUN COMPLETED] success={success} duration={total_duration:.1f}s")
                    print(f"{'='*60}")
                    break

                elif evt_type == "error":
                    err_msg = data.get("error") or data.get("message", "unknown error")
                    errors.append(str(err_msg)[:300])
                    print(f"[ERROR] {err_msg}")

                elif evt_type == "system_info":
                    msg = data.get("message", "")
                    print(f"[INFO] {msg}")

                elif evt_type == "warning":
                    msg = data.get("message", "")
                    warnings.append(msg)
                    print(f"[WARN] {msg}")

            except asyncio.TimeoutError:
                print("[TIMEOUT] No event received in 600s, aborting.")
                errors.append("Test timed out after 600s")
                break
            except Exception as e:
                print(f"[EXCEPTION] {e}")
                errors.append(str(e))
                break

    # Summary report
    total_duration = time.time() - start_time
    print(f"\n{'='*60}")
    print("TEST SUMMARY")
    print(f"{'='*60}")
    print(f"Duration: {total_duration:.1f}s")
    print(f"Nodes: {len(node_events)}")
    for nid, info in node_events.items():
        pvals = info.get("progress_values", [])
        prange = f" progress=[{min(pvals):.0f}-{max(pvals):.0f}]" if pvals else ""
        print(f"  {nid}: {info['agent']} — {info['status']}{prange}")
    print(f"\nErrors: {len(errors)}")
    for e in errors:
        print(f"  - {e}")
    print(f"Warnings: {len(warnings)}")
    for w in warnings:
        print(f"  - {w}")
    print(f"Progress Issues (V4.9.3 clamp): {len(progress_issues)}")
    for p in progress_issues:
        print(f"  - {p}")
    print(f"{'='*60}")

if __name__ == "__main__":
    asyncio.run(run_test())
