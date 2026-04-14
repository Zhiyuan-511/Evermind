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
    monkeypatch.setattr(server, "_acquire_backend_runtime_lock", lambda: None)
    monkeypatch.setattr(server, "_release_backend_runtime_lock", lambda: None)
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
            assert observed["canonical_context"]["state_snapshot"]["difficulty"] == "simple"
            assert observed["canonical_context"]["state_snapshot"]["template_id"] == "simple"
            assert observed["canonical_context"]["state_snapshot"]["node_order"] == ["builder", "deployer", "tester"]

            assert run is not None
            assert run["status"] == "done"
            assert run["summary"] == "Built a small tech landing page"
            assert run["risks"] == ["Minor spacing polish"]

            assert task is not None
            assert task["status"] == "done"
            assert task["latest_summary"] == "Built a small tech landing page"
            assert task["latest_risk"] == "Minor spacing polish"
            assert run_id in task["run_ids"]


def test_run_goal_same_session_marks_session_continuation(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    existing_task = task_store.get_task_store().create_task({
        "title": "已有 3D 游戏项目",
        "description": "old project",
        "session_id": "session_keep_editing",
    })
    task_store.get_task_store().update_task(existing_task["id"], {
        "status": "done",
        "latest_summary": "第三人称射击游戏初版已完成",
    })

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Incremental edit", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "游戏写得还可以，但是还有一点问题，你要详细修复这些问题",
                "difficulty": "standard",
                "model": "gpt-5.4",
                "session_id": "session_keep_editing",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            assert ack["sessionContinuation"] is True
            assert observed["canonical_context"]["session_continuation"] is True
            assert observed["canonical_context"]["state_snapshot"]["session_continuation"] is True
            assert observed["canonical_context"]["state_snapshot"]["previous_task_id"] == existing_task["id"]
            assert "已有 3D 游戏项目" in observed["canonical_context"]["session_context_note"]


def test_run_goal_same_session_continuation_includes_project_memory_digest(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    ts = task_store.get_task_store()
    rs = task_store.get_run_store()
    nes = task_store.get_node_execution_store()
    artifacts = task_store.get_artifact_store()

    existing_task = ts.create_task({
        "title": "已有 TPS 项目",
        "description": "old tps project",
        "session_id": "session_memory_digest",
    })
    ts.update_task(existing_task["id"], {
        "status": "done",
        "latest_summary": "上一轮已经做出可玩的第三人称射击骨架，但鼠标视角和左右移动仍有问题。",
        "latest_risk": "黑屏和输入方向反转在某些分支上仍可能复现。",
        "review_verdict": "reject",
        "review_issues": [
            "mouse pitch/yaw can invert after merge",
            "A/D movement still reverses in some builds",
        ],
        "related_files": ["/tmp/evermind_output/index.html", "/tmp/evermind_output/game.js"],
    })
    existing_run = rs.create_run({
        "task_id": existing_task["id"],
        "runtime": "local",
        "workflow_template_id": "optimize",
        "trigger_source": "optimization_pass",
    })
    ts.link_run(existing_task["id"], existing_run["id"])
    rs.update_run(existing_run["id"], {
        "summary": "上一轮完成了 TPS 基础玩法、HUD 和敌人刷新，但 reviewer 退回了控制与加载问题。",
        "risks": ["merger 分支存在输入映射冲突"],
    })
    builder_node = nes.create_node_execution({
        "run_id": existing_run["id"],
        "node_key": "builder1",
        "node_label": "Builder 1",
    })
    nes.update_node_execution(builder_node["id"], {
        "status": "passed",
        "work_summary": ["修复了相机跟随和平滑插值", "补了子弹命中反馈与可见轨迹"],
        "loaded_skills": ["godogen-playable-loop", "godogen-tps-control-sanity-lock"],
    })
    reviewer_node = nes.create_node_execution({
        "run_id": existing_run["id"],
        "node_key": "reviewer",
        "node_label": "Reviewer",
    })
    rollback_artifact = artifacts.save_artifact({
        "run_id": existing_run["id"],
        "node_execution_id": reviewer_node["id"],
        "artifact_type": "review_rollback_report",
        "title": "Reviewer rollback report",
        "path": "/tmp/reports/reviewer_rollback.md",
        "content": "详细回退报告",
    })
    nes.update_node_execution(reviewer_node["id"], {
        "status": "failed",
        "output_summary": "审查发现左右移动和鼠标方向仍有回归。",
        "latest_review_report_artifact_id": rollback_artifact["id"],
    })

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Continuation with memory digest", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "继续优化这个 TPS 游戏，重点修复鼠标视角、左右移动和黑屏问题",
                "difficulty": "standard",
                "model": "kimi-coding",
                "session_id": "session_memory_digest",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            assert ack["sessionContinuation"] is True
            assert ack["projectMemory"] is True
            assert ack["projectMemoryTaskId"] == existing_task["id"]

            digest = observed["canonical_context"]["project_memory_digest"]
            assert "上一轮已经做出可玩的第三人称射击骨架" in digest
            assert "A/D movement still reverses in some builds" in digest
            assert "Builder 1" in digest
            assert "修复了相机跟随和平滑插值" in digest
            assert "/tmp/reports/reviewer_rollback.md" in digest


def test_run_goal_game_continuation_uses_parallel_optimize_template(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    existing_task = task_store.get_task_store().create_task({
        "title": "已有 TPS 游戏项目",
        "description": "old game",
        "session_id": "session_parallel_optimize",
    })
    task_store.get_task_store().update_task(existing_task["id"], {
        "status": "done",
        "latest_summary": "TPS 原型已完成，需要继续打磨。",
    })

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Parallel optimize flow", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "继续优化这个第三人称射击游戏，修复鼠标视角、左右移动、黑屏和加载问题，并做完整回归测试",
                "difficulty": "standard",
                "model": "kimi-coding",
                "session_id": "session_parallel_optimize",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            assert ack["sessionContinuation"] is True
            assert ack["run"]["workflow_template_id"] == "optimize"
            assert ack["run"]["trigger_source"] == "optimization_pass"
            assert [node["node_key"] for node in ack["nodeExecutions"]] == [
                "planner", "analyst", "builder1", "builder2", "merger", "reviewer", "tester"
            ]
            assert observed["canonical_context"]["is_custom_plan"] is True


def test_run_goal_prefers_explicit_frontend_model_over_saved_default(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}
    monkeypatch.setitem(server._saved_settings, "default_model", "gpt-5.4")
    monkeypatch.setattr(server.AIBridge, "preferred_model_for_node", lambda self, node, fallback_model: fallback_model)

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["model"] = model
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Model respected", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "做一个科技风单页网站",
                "difficulty": "simple",
                "model": "kimi-coding",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            assert observed["model"] == "kimi-coding"
            assert all(node["assigned_model"] == "kimi-coding" for node in ack["nodeExecutions"])


def test_run_goal_same_session_fresh_full_brief_does_not_mark_session_continuation(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    existing_task = task_store.get_task_store().create_task({
        "title": "已有 3D 游戏项目",
        "description": "old project",
        "session_id": "session_fresh_restart",
    })
    task_store.get_task_store().update_task(existing_task["id"], {
        "status": "done",
        "latest_summary": "第三人称射击游戏初版已完成",
    })

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Fresh rebuild", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "创建一个3D第三人称射击游戏，要有怪物、武器、大地图、关卡和通过页面，整体要达到商业级质量。",
                "difficulty": "pro",
                "model": "gpt-5.4",
                "session_id": "session_fresh_restart",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            assert ack["sessionContinuation"] is False
            assert observed["canonical_context"]["session_continuation"] is False
            assert observed["canonical_context"]["state_snapshot"]["session_continuation"] is False
            assert not observed["canonical_context"]["session_context_note"]


def test_run_goal_cross_session_related_memory_handoff_without_session_continuation(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}
    repeated_goal = "创建一个3D第三人称射击游戏，要有怪物、武器、大地图、关卡和通过页面，整体要达到商业级质量。"

    existing_task = task_store.get_task_store().create_task({
        "title": repeated_goal[:80],
        "description": repeated_goal,
        "session_id": "session_old_relay",
    })
    task_store.get_task_store().update_task(existing_task["id"], {
        "status": "done",
        "latest_summary": "上一轮已完成基础 TPS 架构，但 reviewer 指出相机朝向和 render 守卫需要修复。",
        "latest_risk": "W/S 方向感可能和镜头语义冲突。",
        "review_verdict": "reject",
        "review_issues": [
            "camera starts in front of player",
            "renderer.render can execute before renderer init",
        ],
    })

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Fresh session with memory handoff", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": repeated_goal,
                "difficulty": "pro",
                "model": "gpt-5.4",
                "session_id": "session_new_relay",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            assert ack["sessionContinuation"] is False
            assert ack["crossSessionMemory"] is True
            assert ack["crossSessionMemoryTaskId"] == existing_task["id"]

            canonical_context = observed["canonical_context"]
            assert canonical_context["session_continuation"] is False
            assert canonical_context["state_snapshot"]["cross_session_memory_task_id"] == existing_task["id"]
            assert "Recent related run detected" in canonical_context["cross_session_memory_note"]
            assert "TPS" in canonical_context["cross_session_memory_note"]
            assert "camera starts in front of player" in canonical_context["cross_session_memory_note"]


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
            assert nodes[0]["input_summary"].startswith("拆解网站信息架构并明确页面模块顺序")
            assert nodes[1]["input_summary"].startswith("研究 3 个竞品官网并总结视觉方向与转化策略")
            assert nodes[2]["input_summary"].startswith("实现高保真首页，并应用网页美术优化类 skills")
            assert "[RUN GOAL]" in nodes[0]["input_summary"]
            assert "做一个高端 AI 官网" in nodes[0]["input_summary"]
            assert nodes[2]["depends_on_keys"] == ["analyst"]
            assert "research-pattern-extraction" in nodes[1]["loaded_skills"]
            assert "commercial-ui-polish" in nodes[2]["loaded_skills"]
            assert observed["canonical_context"]["node_executions"][0]["input_summary"].startswith("拆解网站信息架构并明确页面模块顺序")
            assert observed["canonical_context"]["node_executions"][1]["input_summary"].startswith("研究 3 个竞品官网并总结视觉方向与转化策略")
            assert observed["canonical_context"]["node_executions"][2]["input_summary"].startswith("实现高保真首页，并应用网页美术优化类 skills")


def test_run_goal_user_canvas_plan_preserves_trigger_source(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        return {"success": True, "summary": "Canvas plan executed", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "继续优化这个 3D 游戏",
                "difficulty": "pro",
                "chat_history": [],
                "plan": {
                    "source": "user_canvas",
                    "nodes": [
                        {"nodeKey": "planner", "nodeLabel": "Planner", "taskDescription": "收敛本轮优化目标"},
                        {"nodeKey": "analyst", "nodeLabel": "Analyst", "taskDescription": "抓取 GitHub 参考并分配给 builders", "dependsOn": ["planner"]},
                        {"nodeKey": "builder1", "nodeLabel": "Builder 1", "taskDescription": "修主循环和交互", "dependsOn": ["analyst"]},
                        {"nodeKey": "builder2", "nodeLabel": "Builder 2", "taskDescription": "修 HUD 和反馈", "dependsOn": ["analyst"]},
                        {"nodeKey": "merger", "nodeLabel": "Merger", "taskDescription": "整合 builder1/builder2 的成果", "dependsOn": ["builder1", "builder2"]},
                        {"nodeKey": "reviewer", "nodeLabel": "Reviewer", "taskDescription": "严格复审", "dependsOn": ["merger"]},
                    ],
                },
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            assert ack["run"]["trigger_source"] == "user_canvas"
            assert [node["node_key"] for node in ack["nodeExecutions"]] == [
                "planner", "analyst", "builder1", "builder2", "merger", "reviewer"
            ]
            assert ack["nodeExecutions"][4]["depends_on_keys"] == ["builder1", "builder2"]


def test_run_goal_session_continuation_uses_optimize_template(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    existing_task = task_store.get_task_store().create_task({
        "title": "已有官网项目",
        "description": "old website",
        "session_id": "session_optimize_flow",
    })
    task_store.get_task_store().update_task(existing_task["id"], {
        "status": "done",
        "latest_summary": "Landing page v1 shipped",
    })

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Optimize flow executed", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "继续优化刚才那个网站的导航和品牌感",
                "difficulty": "standard",
                "model": "gpt-5.4",
                "session_id": "session_optimize_flow",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            assert ack["sessionContinuation"] is True
            assert ack["run"]["workflow_template_id"] == "optimize"
            assert ack["run"]["trigger_source"] == "optimization_pass"
            assert [node["node_key"] for node in ack["nodeExecutions"]] == ["planner", "analyst", "builder", "reviewer"]
            assert observed["canonical_context"]["is_custom_plan"] is True


def test_run_goal_pro_complex_goal_uses_parallel_builder_quality_path(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Luxury site planned", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "做一个介绍奢侈品的八页面网站，页面要像苹果官网一样高级，并带电影感动画转场。",
                "difficulty": "pro",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            nodes = ack["nodeExecutions"]
            assert [node["node_key"] for node in nodes[:6]] == [
                "planner", "analyst", "uidesign", "scribe", "builder1", "builder2"
            ]
            assert nodes[1]["depends_on_keys"] == ["planner"]
            assert nodes[4]["depends_on_keys"] == ["analyst", "uidesign"]
            assert nodes[5]["depends_on_keys"] == ["analyst", "uidesign"]
            assert nodes[6]["depends_on_keys"] == ["builder1", "builder2"]
            assert nodes[7]["depends_on_keys"] == ["merger", "scribe"]
            assert observed["canonical_context"]["node_executions"][4]["depends_on_keys"] == ["analyst", "uidesign"]


def test_run_goal_pro_game_baseline_uses_parallel_integrator_builders(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Third-person shooter planned", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "做一个 3D 第三人称射击游戏，带怪物、武器和大地图。",
                "difficulty": "pro",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            nodes = ack["nodeExecutions"]
            assert [node["node_key"] for node in nodes] == [
                "planner", "analyst", "uidesign", "scribe", "builder1", "builder2", "merger", "reviewer", "deployer", "tester", "debugger"
            ]
            assert observed["canonical_context"]["state_snapshot"]["node_order"] == [
                "planner", "analyst", "uidesign", "scribe", "builder1", "builder2", "merger", "reviewer", "deployer", "tester", "debugger"
            ]


def test_run_goal_pro_commercial_voxel_game_adds_parallel_integrator_asset_pipeline(ws_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    observed = {}

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        observed["canonical_context"] = canonical_context
        return {"success": True, "summary": "Voxel shooter planned", "remaining_risks": []}

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "创建一个我的世界一样的3d像素版射击游戏，要有怪物等等，不同的枪械武器，有关口，和通关胜利页面等等，是一个可以商业用途的3d小游戏",
                "difficulty": "pro",
                "chat_history": [],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "openclaw_run_complete"},
            )

            ack = events["run_goal_ack"][0]["payload"]
            nodes = ack["nodeExecutions"]
            assert [node["node_key"] for node in nodes] == [
                "planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder1", "builder2", "merger", "reviewer", "deployer", "tester", "debugger"
            ]
            assert nodes[1]["depends_on_keys"] == ["planner"]
            assert nodes[5]["depends_on_keys"] == ["analyst", "assetimport"]
            assert nodes[6]["depends_on_keys"] == ["analyst", "assetimport"]
            assert nodes[7]["depends_on_keys"] == ["builder1", "builder2"]
            assert observed["canonical_context"]["state_snapshot"]["node_order"] == [
                "planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder1", "builder2", "merger", "reviewer", "deployer", "tester", "debugger"
            ]


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


def test_run_goal_openclaw_dispatch_includes_attachment_and_session_context(ws_env, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server, "CHAT_UPLOADS_DIR", tmp_path / "uploads")
    server.CHAT_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    session_id = "session_keep_editing"
    session_dir = server.CHAT_UPLOADS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    attachment_path = session_dir / "mood-board.png"
    attachment_path.write_bytes(b"png")

    existing_task = task_store.get_task_store().create_task({
        "title": "已有 3D 游戏项目",
        "description": "old project",
        "session_id": session_id,
    })
    task_store.get_task_store().update_task(existing_task["id"], {
        "status": "done",
        "latest_summary": "第三人称射击游戏初版已完成",
    })

    async def fake_run(self, goal, model, conversation_history=None, difficulty="standard", canonical_context=None):
        raise AssertionError("Local orchestrator should not run in openclaw mode")

    monkeypatch.setattr(server.Orchestrator, "run", fake_run)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ui_ws:
            assert ui_ws.receive_json()["type"] == "connected"

            ui_ws.send_json({
                "type": "run_goal",
                "goal": "继续优化这个游戏的战斗反馈和 HUD",
                "difficulty": "simple",
                "runtime": "openclaw",
                "session_id": session_id,
                "chat_history": [],
                "attachments": [
                    {
                        "id": "att_1",
                        "name": "mood-board.png",
                        "path": str(attachment_path),
                        "mimeType": "image/png",
                        "size": 3,
                    }
                ],
            })

            events = _collect_ws_events(
                ui_ws,
                {"run_goal_ack", "task_created", "run_created", "evermind_dispatch_node"},
            )

            dispatched = events["evermind_dispatch_node"][0]["payload"]
            assert dispatched["runtime"] == "openclaw"
            assert dispatched["sessionId"] == session_id
            assert "[RUN GOAL]" in dispatched["inputSummary"]
            assert "[ATTACHED FILES]" in dispatched["inputSummary"]
            assert "mood-board.png" in dispatched["inputSummary"]
            assert "stored_path:" in dispatched["inputSummary"]
            assert "[SESSION CONTEXT]" in dispatched["inputSummary"]
            assert "已有 3D 游戏项目" in dispatched["inputSummary"]
            assert "mood-board.png" in dispatched["goal"]


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
