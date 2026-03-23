import time
import asyncio

import pytest
from fastapi.testclient import TestClient

import server
import task_store
from connector_idempotency import connector_idempotency


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    monkeypatch.setattr("task_store.STORE_DIR", tmp_path)
    monkeypatch.setattr("task_store.TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr("task_store.REPORTS_FILE", tmp_path / "reports.json")
    monkeypatch.setattr("task_store.RUNS_FILE", tmp_path / "runs.json")
    monkeypatch.setattr("task_store.NODE_EXECUTIONS_FILE", tmp_path / "node_executions.json")
    monkeypatch.setattr("task_store.ARTIFACTS_FILE", tmp_path / "artifacts.json")
    task_store._task_store = None
    task_store._report_store = None
    task_store._run_store = None
    task_store._node_execution_store = None
    task_store._artifact_store = None
    connector_idempotency.clear()
    server.connected_clients.clear()
    server._active_tasks.clear()
    server._detached_tasks.clear()
    server._openclaw_dispatch_watchdogs.clear()
    yield
    server.connected_clients.clear()
    server._active_tasks.clear()
    server._detached_tasks.clear()
    for task in list(server._openclaw_dispatch_watchdogs.values()):
        if not task.done():
            task.cancel()
    server._openclaw_dispatch_watchdogs.clear()
    connector_idempotency.clear()
    task_store._task_store = None
    task_store._report_store = None
    task_store._run_store = None
    task_store._node_execution_store = None
    task_store._artifact_store = None


def _collect_ws_events(ws, wanted_types, *, timeout_s=1.0):
    deadline = time.time() + timeout_s
    bucket = {}
    while time.time() < deadline:
        message = ws.receive_json()
        bucket.setdefault(message["type"], []).append(message)
        if all(event_type in bucket for event_type in wanted_types):
            return bucket
    raise AssertionError(f"Timed out waiting for WS events: {sorted(wanted_types)}; got={sorted(bucket)}")


def test_run_goal_creates_canonical_records_and_projects_terminal_state(ws_env, monkeypatch):
    observed = {}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["goal"] = goal
        observed["model"] = model
        observed["difficulty"] = difficulty
        observed["canonical_context"] = canonical_context
        return {
            "success": True,
            "summary": "Built a small tech landing page",
            "remaining_risks": ["Minor spacing polish"],
        }

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "做一个科技风单页网站",
                "difficulty": "simple",
                "model": "gpt-5.4",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            assert "run_goal_ack" in events
            assert "task_created" in events
            assert "run_created" in events
            assert "openclaw_run_complete" in events

            ack = events["run_goal_ack"][0]
            task_payload = ack["payload"]["task"]
            run_payload = ack["payload"]["run"]
            node_payload = ack["payload"]["nodeExecutions"]

            assert "createdAt" in task_payload
            assert "updatedAt" in task_payload
            assert "runIds" in task_payload
            assert "created_at" not in task_payload

            task_id = ack["payload"]["taskId"]
            run_id = ack["payload"]["runId"]
            assert task_id == task_payload["id"]
            assert run_id == run_payload["id"]
            assert task_id == events["task_created"][0]["payload"]["task"]["id"]
            assert run_id == events["run_created"][0]["payload"]["run"]["id"]
            assert run_id in task_payload["runIds"]
            assert len(node_payload) == 3
            assert run_payload["runtime"] == "local"
            assert run_payload["trigger_source"] == "ui"
            assert run_payload["workflow_template_id"] == "simple"

            completion_payload = events["openclaw_run_complete"][0]["payload"]
            assert completion_payload["runId"] == run_id
            assert completion_payload["taskId"] == task_id
            assert completion_payload["success"] is True
            assert completion_payload["finalResult"] == "success"

            deadline = time.time() + 1.0
            while time.time() < deadline:
                task = task_store.get_task_store().get_task(task_id)
                run = task_store.get_run_store().get_run(run_id)
                if task and run and run["status"] == "done" and task["status"] == "done":
                    break
                time.sleep(0.01)

            task = task_store.get_task_store().get_task(task_id)
            run = task_store.get_run_store().get_run(run_id)

            assert observed["goal"] == "做一个科技风单页网站"
            assert observed["difficulty"] == "simple"
            assert observed["canonical_context"]["task_id"] == task_id
            assert observed["canonical_context"]["run_id"] == run_id
            assert len(observed["canonical_context"]["node_executions"]) == 3

            assert run is not None
            assert run["status"] == "done"
            assert run["summary"] == "Built a small tech landing page"
            assert run["risks"] == ["Minor spacing polish"]

            assert task is not None
            assert task["status"] == "done"
            assert task["latest_summary"] == "Built a small tech landing page"
            assert task["latest_risk"] == "Minor spacing polish"
            assert run_id in task["run_ids"]


def test_connected_payload_includes_openclaw_bundle(ws_env):
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            connected = ui_ws.receive_json()
            assert connected["type"] == "connected"
            openclaw = connected.get("openclaw") or {}
            assert openclaw["ws_url"].endswith("/ws")
            assert openclaw["guide_url"].endswith("/api/openclaw-guide")
            assert openclaw["deep_links"]["open_app"] == "evermind://"
            assert openclaw["mcp_config"]["mcpServers"]["evermind"]["transport"] == "websocket"
            assert "OpenClaw" in openclaw["guide"]


def test_run_goal_custom_plan_preserves_node_task_descriptions(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Custom plan executed", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "做一个高端 AI 官网",
                "difficulty": "standard",
                "chat_history": [],
                "plan": {
                    "nodes": [
                        {
                            "nodeKey": "planner",
                            "nodeLabel": "Website Planner",
                            "taskDescription": "拆解网站信息架构并明确页面模块顺序",
                        },
                        {
                            "nodeKey": "analyst",
                            "nodeLabel": "Reference Analyst",
                            "task": "研究 3 个竞品官网并总结视觉方向与转化策略",
                            "dependsOn": ["planner"],
                        },
                        {
                            "nodeKey": "builder",
                            "nodeLabel": "UI Builder",
                            "description": "实现高保真首页，并应用网页美术优化类 skills",
                            "dependsOn": ["analyst"],
                        },
                    ],
                },
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            nodes = ack["nodeExecutions"]
            assert ack["run"]["trigger_source"] == "openclaw_planner"
            assert [node["node_key"] for node in nodes] == ["planner", "analyst", "builder"]
            assert nodes[0]["input_summary"] == "拆解网站信息架构并明确页面模块顺序"
            assert nodes[1]["input_summary"] == "研究 3 个竞品官网并总结视觉方向与转化策略"
            assert nodes[2]["input_summary"] == "实现高保真首页，并应用网页美术优化类 skills"
            assert nodes[2]["depends_on_keys"] == ["analyst"]
            assert "research-pattern-extraction" in nodes[1]["loaded_skills"]
            assert "commercial-ui-polish" in nodes[2]["loaded_skills"]
            assert observed["canonical_context"]["node_executions"][0]["input_summary"] == "拆解网站信息架构并明确页面模块顺序"
            assert observed["canonical_context"]["node_executions"][1]["input_summary"] == "研究 3 个竞品官网并总结视觉方向与转化策略"
            assert observed["canonical_context"]["node_executions"][2]["input_summary"] == "实现高保真首页，并应用网页美术优化类 skills"


def test_run_goal_completion_backfills_run_metrics_and_preview_url(ws_env, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    preview_file = tmp_path / "index.html"
    preview_file.write_text("<!DOCTYPE html><html><body>ok</body></html>", encoding="utf-8")

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        node_store = task_store.get_node_execution_store()
        first_node_id = canonical_context["node_executions"][0]["id"]
        node_store.transition_node(first_node_id, "running")
        node_store.update_node_execution(first_node_id, {"tokens_used": 222, "cost": 0.45})
        node_store.transition_node(first_node_id, "passed")
        return {
            "success": True,
            "summary": "Backfilled metrics",
            "remaining_risks": [],
        }

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)
    monkeypatch.setattr(server, "latest_preview_artifact", lambda: ("root", preview_file))
    monkeypatch.setattr(server, "build_preview_url_for_file", lambda html_file: "http://127.0.0.1:8765/preview/index.html")

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "做一个科技风单页网站",
                "difficulty": "simple",
                "model": "gpt-5.4",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            completion_payload = events["openclaw_run_complete"][0]["payload"]
            run_id = ack["runId"]

            assert completion_payload["totalTokens"] == 222
            assert completion_payload["totalCost"] == 0.45
            assert completion_payload["previewUrl"] == "http://127.0.0.1:8765/preview/index.html"

            run = task_store.get_run_store().get_run(run_id)
            assert run is not None
            assert run["total_tokens"] == 222
            assert run["total_cost"] == 0.45


def test_run_goal_openclaw_request_dispatches_directly_without_local_orchestrator(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {"calls": 0}

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["calls"] += 1
        return {"success": True, "summary": "Should not run", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "test openclaw fallback",
                "difficulty": "simple",
                "runtime": "openclaw",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "evermind_dispatch_node"},
            )

            assert "run_goal_ack" in events
            assert "task_created" in events
            assert "run_created" in events
            assert "evermind_dispatch_node" in events

            ack = events["run_goal_ack"][0]["payload"]
            assert ack["requestedRuntime"] == "openclaw"
            assert ack["effectiveRuntime"] == "openclaw"
            assert ack["run"]["runtime"] == "openclaw"
            assert observed["calls"] == 0

            run_id = ack["runId"]
            node_executions = ack["nodeExecutions"]
            assert len(node_executions) == 3

            dispatched = events["evermind_dispatch_node"][0]["payload"]
            assert dispatched["runId"] == run_id
            assert dispatched["runtime"] == "openclaw"
            assert dispatched["launchTriggered"] is True
            assert dispatched["nodeKey"] == "builder"

            run = task_store.get_run_store().get_run(run_id)
            node = task_store.get_node_execution_store().get_node_execution(dispatched["nodeExecutionId"])
            assert run is not None
            assert run["status"] == "running"
            assert run["runtime"] == "openclaw"
            assert run["active_node_execution_ids"] == [dispatched["nodeExecutionId"]]
            assert node is not None
            assert node["status"] == "running"


def test_run_goal_openclaw_fails_fast_when_no_runtime_ack_arrives(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server, "OPENCLAW_DISPATCH_ACK_TIMEOUT_S", 1)

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        raise AssertionError("Local orchestrator should not run in openclaw mode")

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "test openclaw missing runtime",
                "difficulty": "simple",
                "runtime": "openclaw",
                "chat_history": [],
            })

            ack_payload = None
            dispatch_payload = None
            for _ in range(5):
                message = ui_ws.receive_json()
                if message["type"] == "run_goal_ack":
                    ack_payload = message["payload"]
                elif message["type"] == "evermind_dispatch_node":
                    dispatch_payload = message["payload"]

            assert ack_payload is not None
            assert dispatch_payload is not None

            run_payload = ack_payload["run"]
            run_id = ack_payload["runId"]
            dispatched_node_id = dispatch_payload["nodeExecutionId"]

            assert run_payload["runtime"] == "openclaw"

            deadline = time.time() + 2.0
            while time.time() < deadline:
                run = task_store.get_run_store().get_run(run_id)
                node = task_store.get_node_execution_store().get_node_execution(dispatched_node_id)
                if run and node and run["status"] == "failed" and node["status"] == "failed":
                    break
                time.sleep(0.01)

            run = task_store.get_run_store().get_run(run_id)
            node = task_store.get_node_execution_store().get_node_execution(dispatched_node_id)
            assert run is not None
            assert run["status"] == "failed"
            assert "ack/progress" in run["summary"]
            assert node is not None
            assert node["status"] == "failed"
            assert "ack/progress" in node["error_message"]


def test_run_goal_local_continues_after_sender_disconnect(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {"completed": False, "cancelled": False}

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        try:
            await asyncio.sleep(0.05)
            observed["completed"] = True
            return {
                "success": True,
                "summary": "Detached run finished",
                "remaining_risks": [],
            }
        except asyncio.CancelledError:
            observed["cancelled"] = True
            raise

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as monitor_ws:
            assert monitor_ws.receive_json()["type"] == "connected"

            with client.websocket_connect("/ws") as sender_ws:
                assert sender_ws.receive_json()["type"] == "connected"
                sender_ws.send_json({
                    "type": "run_goal",
                    "goal": "做一个科技风单页网站",
                    "difficulty": "simple",
                    "model": "gpt-5.4",
                    "chat_history": [],
                })
                sender_ack = sender_ws.receive_json()
                assert sender_ack["type"] == "run_goal_ack"
                run_id = sender_ack["payload"]["runId"]
                task_id = sender_ack["payload"]["taskId"]

            events = _collect_ws_events(
                monitor_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
                timeout_s=2.0,
            )

            completion_payload = events["openclaw_run_complete"][0]["payload"]
            assert completion_payload["runId"] == run_id
            assert completion_payload["taskId"] == task_id
            assert completion_payload["success"] is True
            assert completion_payload["finalResult"] == "success"

            run = task_store.get_run_store().get_run(run_id)
            task = task_store.get_task_store().get_task(task_id)
            assert run is not None
            assert task is not None
            assert run["status"] == "done"
            assert task["status"] == "done"
            assert observed["completed"] is True
            assert observed["cancelled"] is False


def test_run_goal_ack_broadcast_survives_sender_disconnect_during_ack(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {"completed": False, "cancelled": False}

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        try:
            await asyncio.sleep(0.05)
            observed["completed"] = True
            return {
                "success": True,
                "summary": "Detached run finished",
                "remaining_risks": [],
            }
        except asyncio.CancelledError:
            observed["cancelled"] = True
            raise

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as monitor_ws:
            assert monitor_ws.receive_json()["type"] == "connected"

            with client.websocket_connect("/ws") as sender_ws:
                assert sender_ws.receive_json()["type"] == "connected"
                sender_ws.send_json({
                    "type": "run_goal",
                    "goal": "做一个科技风单页网站",
                    "difficulty": "simple",
                    "model": "gpt-5.4",
                    "chat_history": [],
                })
                sender_ws.close()

            events = _collect_ws_events(
                monitor_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
                timeout_s=2.0,
            )

            ack = events["run_goal_ack"][0]["payload"]
            completion_payload = events["openclaw_run_complete"][0]["payload"]
            run_id = ack["runId"]
            task_id = ack["taskId"]

            assert completion_payload["runId"] == run_id
            assert completion_payload["taskId"] == task_id
            assert completion_payload["success"] is True
            assert completion_payload["finalResult"] == "success"

            run = task_store.get_run_store().get_run(run_id)
            task = task_store.get_task_store().get_task(task_id)
            assert run is not None
            assert task is not None
            assert run["status"] == "done"
            assert task["status"] == "done"
            assert observed["completed"] is True
            assert observed["cancelled"] is False
