#!/usr/bin/env python3
"""
Sync the desktop Evermind runtime, optionally restart the app, send a run_goal
over WebSocket, and monitor the run until it reaches a terminal state.

Usage example:
  python3 backend/scripts/desktop_run_goal_monitor.py \
    --sync-runtime \
    --restart-app \
    --goal "创建一个我的世界一样的3d像素版射击游戏..." \
    --difficulty pro \
    --model gpt-5.4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

try:
    import websockets
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: websockets. Install backend requirements first."
    ) from exc


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APP = Path.home() / "Desktop" / "Evermind.app"
DEFAULT_WS_URL = "ws://127.0.0.1:8765/ws"
DEFAULT_STORE_DIR = Path.home() / ".evermind"
DEFAULT_BACKEND_LOG = DEFAULT_STORE_DIR / "logs" / "evermind-backend.log"
TERMINAL_RUN_STATES = {"done", "failed", "cancelled", "waiting_review"}

RUNTIME_FILES = [
    "agent_skills.py",
    "ai_bridge.py",
    "config_utils.py",
    "connector_idempotency.py",
    "executor.py",
    "html_postprocess.py",
    "knowledge_base.py",
    "node_roles.py",
    "orchestrator.py",
    "preview_validation.py",
    "privacy.py",
    "proxy_relay.py",
    "repo_map.py",
    "runtime_paths.py",
    "server.py",
    "settings.py",
    "task_classifier.py",
    "task_store.py",
    "workflow_templates.py",
    "requirements.txt",
]

RUNTIME_DIRS = [
    "agent_skills",
    "plugins",
    "templates",
]


def _print(msg: str) -> None:
    print(msg, flush=True)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_retry(path: Path, *, timeout_s: float = 3.0) -> Any:
    deadline = time.time() + timeout_s
    last_error: Optional[Exception] = None
    while time.time() < deadline:
        try:
            return _load_json(path)
        except Exception as exc:  # pragma: no cover - retry path
            last_error = exc
            time.sleep(0.1)
    raise last_error or RuntimeError(f"Failed to read JSON: {path}")


def _sync_runtime(app_path: Path) -> None:
    target_backend = app_path / "Contents" / "Resources" / "backend"
    source_backend = REPO_ROOT / "backend"
    if not target_backend.exists():
        raise SystemExit(f"Desktop runtime backend not found: {target_backend}")

    _print(f"[sync] source={source_backend}")
    _print(f"[sync] target={target_backend}")

    for rel in RUNTIME_FILES:
        src = source_backend / rel
        dst = target_backend / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    for rel in RUNTIME_DIRS:
        src = source_backend / rel
        dst = target_backend / rel
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)

    _print("[sync] runtime sync complete")


def _restart_app(app_path: Path) -> None:
    app_name = app_path.stem
    _print(f"[app] restarting {app_name}")
    subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to quit'],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    try:
        subprocess.run(["open", str(app_path)], check=True)
        _print("[app] open command sent")
        return
    except subprocess.CalledProcessError as exc:
        _print(f"[app] open failed, falling back to direct executable launch: {exc}")

    executable = app_path / "Contents" / "MacOS" / app_name
    if not executable.exists():
        raise RuntimeError(f"App executable missing: {executable}")
    subprocess.Popen(
        [str(executable)],
        cwd=str(app_path / "Contents" / "Resources"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _print("[app] executable launch sent")


async def _wait_for_ws(ws_url: str, *, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    last_error: Optional[Exception] = None
    while time.time() < deadline:
        try:
            async with websockets.connect(ws_url, open_timeout=5) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                if msg.get("type") == "connected":
                    _print("[ws] backend is ready")
                    return
        except Exception as exc:  # pragma: no cover - retry path
            last_error = exc
            await asyncio.sleep(1)
    raise RuntimeError(f"WebSocket not ready: {ws_url} ({last_error})")


def _start_repo_backend() -> None:
    backend_dir = REPO_ROOT / "backend"
    log_dir = DEFAULT_STORE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "desktop_run_goal_monitor_backend.log"
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=str(backend_dir),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    _print(f"[backend] started repo backend, log={log_path}")


def _find_run(run_id: str, store_dir: Path) -> Optional[Dict[str, Any]]:
    path = store_dir / "runs.json"
    if not path.exists():
        return None
    obj = _load_json_retry(path)
    runs = obj if isinstance(obj, list) else obj.get("runs", [])
    return next((item for item in runs if item.get("id") == run_id), None)


def _iter_run_nodes(run_id: str, store_dir: Path) -> Iterable[Dict[str, Any]]:
    path = store_dir / "node_executions.json"
    if not path.exists():
        return []
    obj = _load_json_retry(path)
    nodes = obj if isinstance(obj, list) else obj.get("executions", [])
    return [item for item in nodes if item.get("run_id") == run_id]


def _format_node(node: Dict[str, Any]) -> str:
    key = str(node.get("node_key") or node.get("node_type") or "")
    status = str(node.get("status") or "")
    model = str(node.get("assigned_model") or node.get("model") or "")
    phase = str(node.get("phase") or "")
    progress = int(node.get("progress") or 0)
    return f"{key:<12} {status:<10} {progress:>3}% {phase:<14} {model}"


def _node_sort_key(node: Dict[str, Any]) -> tuple[str, str]:
    return (str(node.get("created_at") or ""), str(node.get("node_key") or node.get("node_type") or ""))


def _print_terminal_summary(run_id: str, store_dir: Path) -> None:
    run = _find_run(run_id, store_dir)
    nodes = sorted(_iter_run_nodes(run_id, store_dir), key=_node_sort_key)
    _print("[summary]")
    if run:
        _print(
            f"  run_id={run_id} status={run.get('status')} "
            f"current={run.get('current_node_execution_id')} active={run.get('active_node_execution_ids')}"
        )
    else:
        _print(f"  run_id={run_id} (run record missing)")

    for node in nodes:
        _print("  " + _format_node(node))
        error = str(node.get("error_message") or "").strip()
        if error:
            _print(f"    error: {error[:500]}")
        output_summary = str(node.get("output_summary") or "").strip()
        if output_summary:
            _print(f"    summary: {output_summary[:500]}")


def _tail_log_file(path: Path, line_count: int) -> None:
    if line_count <= 0:
        return
    if not path.exists():
        _print(f"[log] missing: {path}")
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        _print(f"[log] failed to read {path}: {exc}")
        return
    _print(f"[log] tail {min(line_count, len(lines))} lines from {path}")
    for line in lines[-line_count:]:
        _print(line)


async def _send_run_goal(
    ws_url: str,
    *,
    goal: str,
    difficulty: str,
    model: str,
    runtime: str,
    openclaw_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    session_id = f"desktop-monitor-{uuid.uuid4().hex[:8]}"
    async with websockets.connect(ws_url, open_timeout=10) as ws:
        connected = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if connected.get("type") != "connected":
            raise RuntimeError(f"Unexpected first WS message: {connected}")

        payload: Dict[str, Any] = {
            "type": "run_goal",
            "goal": goal,
            "difficulty": difficulty,
            "model": model,
            "runtime": runtime,
            "chat_history": [],
            "session_id": session_id,
        }
        if openclaw_plan:
            payload["plan"] = openclaw_plan

        _print(f"[ws] sending run_goal: difficulty={difficulty} runtime={runtime} model={model}")
        await ws.send(json.dumps(payload, ensure_ascii=False))

        ack_payload: Optional[Dict[str, Any]] = None
        started = time.time()
        while time.time() - started < 30:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            msg = json.loads(raw)
            msg_type = str(msg.get("type") or "")
            if msg_type == "run_goal_ack":
                ack_payload = msg.get("payload") or {}
                break
            if msg_type in {"system_info", "phase_change"}:
                _print(f"[ws:{msg_type}] {str(msg.get('message') or msg.get('payload') or '')[:300]}")

        if not ack_payload:
            raise RuntimeError("Did not receive run_goal_ack")

        run_id = str(ack_payload.get("runId") or "")
        _print(f"[ack] run_id={run_id} task_id={ack_payload.get('taskId')}")
        node_execs = ack_payload.get("nodeExecutions") or []
        _print("[ack] node order: " + " -> ".join(str(node.get("node_key") or "") for node in node_execs))
        return ack_payload


async def _monitor_run(run_id: str, store_dir: Path, *, timeout_s: int) -> int:
    deadline = time.time() + timeout_s
    last_run_state = None
    last_node_snapshot = None

    while time.time() < deadline:
        run = _find_run(run_id, store_dir)
        nodes = sorted(_iter_run_nodes(run_id, store_dir), key=lambda item: str(item.get("created_at") or ""))
        if run:
            run_state = (
                run.get("status"),
                tuple(run.get("active_node_execution_ids") or []),
                run.get("current_node_execution_id"),
                run.get("updated_at"),
            )
            if run_state != last_run_state:
                _print(
                    f"[run] status={run.get('status')} current={run.get('current_node_execution_id')} "
                    f"active={run.get('active_node_execution_ids')}"
                )
                last_run_state = run_state

        node_snapshot = tuple(
            (node.get("node_key"), node.get("status"), node.get("progress"), node.get("phase"), node.get("assigned_model"))
            for node in nodes
        )
        if node_snapshot != last_node_snapshot:
            _print("[nodes]")
            for node in nodes:
                _print("  " + _format_node(node))
                error = str(node.get("error_message") or "").strip()
                if error:
                    _print(f"    error: {error[:240]}")
            last_node_snapshot = node_snapshot

        if run and str(run.get("status") or "") in TERMINAL_RUN_STATES:
            _print(f"[done] terminal run status={run.get('status')}")
            _print_terminal_summary(run_id, store_dir)
            return 0 if str(run.get("status")) in {"done", "waiting_review"} else 1

        await asyncio.sleep(2)

    _print("[done] monitor timeout reached")
    _print_terminal_summary(run_id, store_dir)
    return 2


async def _main_async(args: argparse.Namespace) -> int:
    app_path = Path(args.app_path).expanduser()
    if args.sync_runtime:
        _sync_runtime(app_path)

    if args.restart_app:
        _restart_app(app_path)

    try:
        await _wait_for_ws(args.ws_url, timeout_s=args.ws_ready_timeout)
    except Exception:
        if not args.start_backend_fallback:
            raise
        _start_repo_backend()
        await _wait_for_ws(args.ws_url, timeout_s=args.ws_ready_timeout)
    ack = await _send_run_goal(
        args.ws_url,
        goal=args.goal,
        difficulty=args.difficulty,
        model=args.model,
        runtime=args.runtime,
    )
    if args.stop_after_ack:
        _print("[done] stop_after_ack enabled")
        return 0
    run_id = str(ack.get("runId") or "")
    if not run_id:
        raise RuntimeError("run_goal_ack missing runId")
    exit_code = await _monitor_run(run_id, Path(args.store_dir).expanduser(), timeout_s=args.monitor_timeout)
    if exit_code != 0 and args.tail_log_lines > 0:
        _tail_log_file(Path(args.backend_log).expanduser(), args.tail_log_lines)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Desktop Evermind run_goal monitor")
    parser.add_argument("--goal", required=True, help="Goal text to send via run_goal")
    parser.add_argument("--difficulty", default="pro", choices=["simple", "standard", "pro"])
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--runtime", default="local", choices=["local", "openclaw"])
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL)
    parser.add_argument("--store-dir", default=str(DEFAULT_STORE_DIR))
    parser.add_argument("--app-path", default=str(DEFAULT_APP))
    parser.add_argument("--ws-ready-timeout", type=int, default=60)
    parser.add_argument("--monitor-timeout", type=int, default=1800)
    parser.add_argument("--sync-runtime", action="store_true")
    parser.add_argument("--restart-app", action="store_true")
    parser.add_argument("--stop-after-ack", action="store_true")
    parser.add_argument("--start-backend-fallback", action="store_true")
    parser.add_argument("--backend-log", default=str(DEFAULT_BACKEND_LOG))
    parser.add_argument("--tail-log-lines", type=int, default=80)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:  # pragma: no cover
        _print("[exit] interrupted")
        return 130
    except Exception as exc:
        _print(f"[error] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
