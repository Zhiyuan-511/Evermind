"""Evermind MCP server (v7.21) — exposes Evermind run-goal pipeline to
external MCP clients (Claude Code / Cursor / Codex / Cline).

Transport: stdio (the default for MCP servers).

Tools:
  - evermind_run_goal(goal, difficulty="standard", template="pro")
        Submit a new goal to the running Evermind backend; returns the
        new run_id immediately. The backend executes the goal in the
        background; poll with evermind_get_run_status until status=done.
  - evermind_list_runs(limit=5)
        List recent runs with status + elapsed time.
  - evermind_get_run_status(run_id)
        Get current status + per-node progress for a run.
  - evermind_get_preview_url(run_id)
        Get the live preview URL for a finished/in-progress run.
  - evermind_cancel_run(run_id)
        Cancel a still-running run.

Prerequisites:
  - Evermind desktop app must be running (backend on http://127.0.0.1:8765)
  - mcp python SDK installed (pip install mcp)

Install for Claude Code:
  claude mcp add evermind \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /path/to/evermind/backend/scripts/mcp_server.py
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from typing import Any, Dict
from urllib import request as _urllib_request
from urllib import error as _urllib_error

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    sys.stderr.write(f"FATAL: mcp python SDK not installed. Run: pip3 install mcp\n  {e}\n")
    sys.exit(1)

EVERMIND_HTTP = "http://127.0.0.1:8765"
EVERMIND_WS = "ws://127.0.0.1:8765/ws"

mcp = FastMCP("evermind")


def _http_get(path: str, timeout: float = 10.0) -> Dict[str, Any]:
    url = EVERMIND_HTTP + path
    try:
        with _urllib_request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (_urllib_error.URLError, _urllib_error.HTTPError, socket.timeout, json.JSONDecodeError) as e:
        return {"_error": f"GET {path}: {e}"}


def _http_post(path: str, body: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    url = EVERMIND_HTTP + path
    data = json.dumps(body).encode("utf-8")
    req = _urllib_request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with _urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (_urllib_error.URLError, _urllib_error.HTTPError, socket.timeout, json.JSONDecodeError) as e:
        return {"_error": f"POST {path}: {e}"}


def _backend_alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1.5):
            return True
    except (socket.error, OSError):
        return False


async def _ws_run_goal(goal: str, difficulty: str, model: str) -> Dict[str, Any]:
    """Send a run_goal command via WebSocket. Waits for run_goal_ack to get run_id."""
    try:
        import websockets
    except ImportError:
        return {"_error": "websockets package not installed; run: pip3 install websockets"}

    msg = {
        "type": "run_goal",
        "goal": goal,
        "difficulty": difficulty,
        "model": model,
        "runtime": "local",
        "chat_history": [],
        "attachments": [],
        "trigger_source": "mcp",
    }

    try:
        async with websockets.connect(EVERMIND_WS, max_size=10_000_000) as ws:
            await ws.send(json.dumps(msg))
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = payload.get("type", "")
                if t == "run_goal_ack" or "run" in payload.get("payload", {}):
                    run_obj = payload.get("payload", {}).get("run") or payload.get("run") or {}
                    if run_obj.get("id"):
                        return {"run_id": run_obj["id"], "task_id": run_obj.get("task_id"),
                                "status": run_obj.get("status"), "ack": payload.get("type")}
                if t == "run_created":
                    run_obj = payload.get("payload", {}).get("run") or {}
                    if run_obj.get("id"):
                        return {"run_id": run_obj["id"], "task_id": run_obj.get("task_id"),
                                "status": run_obj.get("status"), "ack": "run_created"}
            return {"_error": "timed out waiting for run_goal_ack (30s)"}
    except Exception as e:
        return {"_error": f"WS connection failed: {e}"}


@mcp.tool()
def evermind_run_goal(goal: str, difficulty: str = "standard", template: str = "pro") -> Dict[str, Any]:
    """Submit a new goal to Evermind's multi-agent pipeline.

    Args:
      goal: Natural-language description of what to build. Examples:
        - "Build a calculator with basic arithmetic"
        - "Create a 3D first-person shooter game with Three.js"
        - "Design a portfolio website with hero + project cards"
      difficulty: "simple" | "standard" | "pro" | "ultra" (default: "standard").
        "pro" enables the full 12-node parallel pipeline (planner → analyst →
        imagegen + spritesheet + assetimport + builder1 + builder2 → merger
        → reviewer → deployer + debugger + patcher).
      template: alias for difficulty when called via MCP. If both supplied,
        difficulty wins.

    Returns:
      {"run_id": "...", "task_id": "...", "status": "...", "ack": "..."}
      or {"_error": "..."} on failure. Poll evermind_get_run_status(run_id)
      until status == "done" (or "failed").
    """
    if not _backend_alive():
        return {"_error": "Evermind backend (port 8765) not reachable. Open the Evermind desktop app first."}
    if not goal or not goal.strip():
        return {"_error": "goal must be a non-empty string"}
    diff = (difficulty or template or "standard").strip().lower()
    if diff not in ("simple", "standard", "pro", "ultra"):
        diff = "standard"
    return asyncio.run(_ws_run_goal(goal.strip(), diff, ""))


@mcp.tool()
def evermind_list_runs(limit: int = 5) -> Dict[str, Any]:
    """List recent runs (most recent first).

    Args:
      limit: how many runs to return (default 5, max 50).

    Returns:
      {"runs": [{"id", "status", "started_min_ago", "duration_min", "summary"}, ...]}
    """
    if not _backend_alive():
        return {"_error": "Evermind backend not reachable"}
    n = max(1, min(int(limit or 5), 50))
    raw = _http_get(f"/api/runs?limit={n}")
    if "_error" in raw:
        return raw
    out = []
    now = time.time()
    for r in raw.get("runs", []):
        started = r.get("started_at") or 0
        ended = r.get("ended_at") or 0
        out.append({
            "id": r.get("id"),
            "status": r.get("status"),
            "started_min_ago": round((now - started) / 60.0, 1) if started else None,
            "duration_min": round((ended - started) / 60.0, 1) if ended else None,
            "summary": (r.get("summary") or "")[:200],
            "template": r.get("workflow_template_id"),
        })
    return {"runs": out}


@mcp.tool()
def evermind_get_run_status(run_id: str) -> Dict[str, Any]:
    """Get current status + per-node progress for a run.

    Args:
      run_id: the run id returned by evermind_run_goal.

    Returns:
      {"id", "status", "elapsed_min", "summary", "nodes": [{"key", "status",
       "duration_sec", "phase"}, ...], "preview_url"}
    """
    if not _backend_alive():
        return {"_error": "Evermind backend not reachable"}
    rid = (run_id or "").strip()
    if not rid:
        return {"_error": "run_id required"}
    run_raw = _http_get(f"/api/runs/{rid}")
    if "_error" in run_raw:
        return run_raw
    r = run_raw.get("run", {})
    nes_raw = _http_get(f"/api/node-executions?run_id={rid}")
    now = time.time()
    nodes = []
    for ne in (nes_raw.get("nodeExecutions") or [])[:20]:
        sa = ne.get("started_at") or 0
        ea = ne.get("ended_at") or 0
        dur = (ea - sa) if ea else (now - sa) if sa else 0
        nodes.append({
            "key": ne.get("node_key"),
            "status": ne.get("status"),
            "duration_sec": round(dur, 0),
            "phase": (ne.get("phase") or "")[:60],
        })
    started = r.get("started_at") or 0
    return {
        "id": r.get("id"),
        "status": r.get("status"),
        "elapsed_min": round((now - started) / 60.0, 2) if started else None,
        "summary": (r.get("summary") or "")[:300],
        "nodes": nodes,
        "preview_url": f"http://127.0.0.1:8765/preview/{r.get('task_id')}/" if r.get("task_id") else None,
    }


@mcp.tool()
def evermind_get_preview_url(run_id: str) -> Dict[str, Any]:
    """Get the live preview URL for a run's deployed artifact.

    Args:
      run_id: the run id.

    Returns:
      {"preview_url": "http://127.0.0.1:8765/preview/...", "artifact_size_kb": N}
    """
    status = evermind_get_run_status(run_id)
    if "_error" in status:
        return status
    return {"preview_url": status.get("preview_url"), "status": status.get("status"),
            "summary": status.get("summary")}


@mcp.tool()
def evermind_cancel_run(run_id: str) -> Dict[str, Any]:
    """Cancel a running run.

    Args:
      run_id: the run id to cancel.

    Returns:
      {"success": True, "run": {...}} or {"_error": "..."}.
    """
    if not _backend_alive():
        return {"_error": "Evermind backend not reachable"}
    rid = (run_id or "").strip()
    if not rid:
        return {"_error": "run_id required"}
    return _http_post(f"/api/runs/{rid}/cancel", {})


if __name__ == "__main__":
    mcp.run()
