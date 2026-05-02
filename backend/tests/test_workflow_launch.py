"""
Tests for P2: Workflow templates, launch endpoint.
"""
import asyncio
import pytest
import sys
import os
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server
import task_store
from connector_idempotency import connector_idempotency
from workflow_templates import (
    get_template, list_templates, template_nodes,
)


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
    monkeypatch.setattr(server, "_acquire_backend_runtime_lock", lambda: None)
    monkeypatch.setattr(server, "_release_backend_runtime_lock", lambda: None)
    yield
    server.connected_clients.clear()
    server._active_tasks.clear()
    connector_idempotency.clear()
    task_store._task_store = None
    task_store._report_store = None
    task_store._run_store = None
    task_store._node_execution_store = None
    task_store._artifact_store = None


class TestWorkflowTemplates:
    """P2-A: workflow_templates module."""

    def test_list_templates_returns_three(self):
        tpls = list_templates()
        assert len(tpls) == 3
        ids = {t["id"] for t in tpls}
        assert ids == {"simple", "standard", "pro"}

    def test_get_template_standard(self):
        tpl = get_template("standard")
        assert tpl is not None
        assert len(tpl["nodes"]) == 4
        assert tpl["nodes"][0]["key"] == "builder"

    def test_get_template_simple(self):
        tpl = get_template("simple")
        assert tpl is not None
        assert len(tpl["nodes"]) == 3

    def test_get_template_pro(self):
        tpl = get_template("pro")
        assert tpl is not None
        assert len(tpl["nodes"]) == 9
        keys = [n["key"] for n in tpl["nodes"]]
        assert keys[0] == "planner"
        assert "analyst" in keys
        assert "builder1" in keys
        assert "builder2" in keys
        assert "merger" in keys

    def test_get_template_pro_complex_goal_prefers_parallel_builder_quality_path(self):
        tpl = get_template(
            "pro",
            goal="做一个介绍奢侈品的八页面网站，页面要像苹果官网一样高级，并带电影感动画转场。",
        )
        assert tpl is not None
        assert len(tpl["nodes"]) == 12
        assert [node["key"] for node in tpl["nodes"][:6]] == [
            "planner", "analyst", "uidesign", "scribe", "builder1", "builder2"
        ]
        assert tpl["nodes"][1]["depends_on"] == ["planner"]
        assert tpl["nodes"][4]["depends_on"] == ["analyst", "uidesign"]
        assert tpl["nodes"][5]["depends_on"] == ["analyst", "uidesign"]
        assert tpl["nodes"][6]["key"] == "merger"
        assert tpl["nodes"][6]["depends_on"] == ["builder1", "builder2"]
        assert tpl["nodes"][7]["key"] == "polisher"
        assert tpl["nodes"][7]["depends_on"] == ["merger", "scribe"]

    def test_get_template_pro_asset_heavy_goal_expands_to_twelve_nodes(self):
        tpl = get_template(
            "pro",
            goal="做一个奢侈品 lookbook 网站，8 页，包含 hero 插画、lookbook 视觉素材和高质量 asset pack。",
        )
        assert tpl is not None
        assert len(tpl["nodes"]) == 12
        assert [node["key"] for node in tpl["nodes"][:6]] == [
            "planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder1"
        ]

    def test_get_template_pro_game_baseline_uses_parallel_integrator_builders(self):
        tpl = get_template(
            "pro",
            goal="做一个 3D 第三人称射击游戏，带怪物、武器和大地图。",
        )
        assert tpl is not None
        assert [node["key"] for node in tpl["nodes"]] == [
            "planner", "analyst", "uidesign", "scribe", "builder1", "builder2", "merger", "reviewer", "deployer", "tester", "debugger"
        ]
        assert tpl["nodes"][1]["depends_on"] == ["planner"]
        assert tpl["nodes"][4]["depends_on"] == ["analyst", "uidesign", "scribe"]
        assert tpl["nodes"][5]["depends_on"] == ["analyst", "uidesign", "scribe"]
        assert tpl["nodes"][6]["depends_on"] == ["builder1", "builder2"]

    def test_get_template_pro_voxel_game_uses_parallel_integrator_asset_pipeline(self):
        tpl = get_template(
            "pro",
            goal="创建一个我的世界风格的像素设计游戏（3d),地图丰富，要有怪物，机制等等，这款游戏要达到商业级水准，建模之类的都要有",
        )
        assert tpl is not None
        assert [node["key"] for node in tpl["nodes"]] == [
            "planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder1", "builder2", "merger", "reviewer", "deployer", "tester", "debugger"
        ]
        assert tpl["nodes"][5]["depends_on"] == ["analyst", "assetimport"]
        assert tpl["nodes"][6]["depends_on"] == ["analyst", "assetimport"]
        assert tpl["nodes"][7]["depends_on"] == ["builder1", "builder2"]

    def test_get_template_pro_explicit_3d_asset_goal_adds_parallel_integrator_pipeline(self):
        tpl = get_template(
            "pro",
            goal="做一个3d射击游戏，并生成角色模型、武器模型、怪物模型、贴图和asset pack",
        )
        assert tpl is not None
        assert [node["key"] for node in tpl["nodes"][:6]] == [
            "planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder1"
        ]
        assert tpl["nodes"][6]["key"] == "builder2"
        assert tpl["nodes"][6]["depends_on"] == ["analyst", "assetimport"]
        assert tpl["nodes"][7]["key"] == "merger"
        assert tpl["nodes"][7]["depends_on"] == ["builder1", "builder2"]
        assert tpl["nodes"][8]["key"] == "reviewer"

    def test_get_template_pro_concept_asset_goal_adds_pipeline(self):
        tpl = get_template(
            "pro",
            goal="创建一个第三人称3D射击游戏，必须先生成角色、怪物、步枪和场景的3D概念资产包，再生成可玩的HTML成品。",
        )
        assert tpl is not None
        assert [node["key"] for node in tpl["nodes"][:6]] == [
            "planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder1"
        ]
        assert tpl["nodes"][7]["key"] == "merger"

    def test_get_template_pro_presentation_short_goal_routes_dual_builders_through_merger(self):
        tpl = get_template(
            "pro",
            goal="做一个产品发布会PPT",
        )
        assert tpl is not None
        key_map = {node["key"]: node for node in tpl["nodes"]}
        assert "builder1" in key_map and "builder2" in key_map
        assert key_map["builder2"]["depends_on"] == ["builder1"]
        assert "merger" in key_map
        assert key_map["merger"]["depends_on"] == ["builder1", "builder2"]
        assert key_map["reviewer"]["depends_on"] == ["merger"]
        assert key_map["deployer"]["depends_on"] == ["merger"]

    def test_get_template_pro_presentation_design_goal_routes_polisher_through_merger(self):
        tpl = get_template(
            "pro",
            goal="做一个商业级产品发布会PPT演示，视觉高级，转场流畅，适配桌面和移动端",
        )
        assert tpl is not None
        key_map = {node["key"]: node for node in tpl["nodes"]}
        assert "builder1" in key_map and "builder2" in key_map
        assert "polisher" in key_map
        assert "merger" in key_map
        assert key_map["merger"]["depends_on"] == ["builder1", "builder2"]
        assert key_map["polisher"]["depends_on"] == ["merger"]
        assert key_map["reviewer"]["depends_on"] == ["polisher"]
        assert key_map["deployer"]["depends_on"] == ["polisher"]

    def test_get_template_optimize_small_patch_stays_fast_and_patch_first(self):
        tpl = get_template(
            "optimize",
            goal="继续优化刚才那个网站的导航间距和 CTA 文案",
        )
        assert tpl is not None
        assert [node["key"] for node in tpl["nodes"]] == ["builder", "reviewer"]
        assert tpl["profile"]["mode"] == "patch_fast"

    def test_get_template_optimize_game_continuation_uses_parallel_merger_and_tester(self):
        tpl = get_template(
            "optimize",
            goal="继续优化这个第三人称射击游戏，修复鼠标视角和左右移动方向，同时检查黑屏、加载、射击和回归问题",
        )
        assert tpl is not None
        assert [node["key"] for node in tpl["nodes"]] == [
            "planner", "analyst", "builder1", "builder2", "merger", "reviewer", "tester"
        ]
        assert tpl["profile"]["mode"] == "parallel_optimize"
        assert tpl["nodes"][4]["depends_on"] == ["builder1", "builder2"]
        assert tpl["nodes"][6]["depends_on"] == ["reviewer"]

    def test_get_template_unknown_returns_none(self):
        assert get_template("nonexistent") is None

    def test_difficulty_aliases(self):
        assert get_template("fast") == get_template("simple")
        assert get_template("balanced") == get_template("standard")
        assert get_template("advanced") == get_template("pro")

    def test_template_nodes_returns_correct_list(self):
        nodes = template_nodes("standard")
        assert len(nodes) == 4
        assert nodes[0]["key"] == "builder"

    def test_template_nodes_unknown_returns_empty(self):
        assert template_nodes("nonexistent") == []


class TestWorkflowTemplatesEndpoint:
    """P2-A: GET /api/workflow-templates."""

    def test_lists_templates(self, ws_env):
        with TestClient(server.app) as client:
            resp = client.get("/api/workflow-templates")
            assert resp.status_code == 200
            data = resp.json()
            assert "templates" in data
            assert len(data["templates"]) == 3
            ids = {t["id"] for t in data["templates"]}
            assert ids == {"simple", "standard", "pro"}


class TestLaunchEndpoint:
    """P2-B: POST /api/runs/launch."""

    def test_launch_standard_creates_4_nodes(self, ws_env):
        # Create a task first
        created = asyncio.run(server.create_task({"title": "Launch Test"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "standard",
                "runtime": "openclaw",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["templateId"] == "standard"
            assert len(data["nodeExecutions"]) == 4

            run = data["run"]
            assert run["status"] == "running"
            assert run["runtime"] == "openclaw"
            assert run["workflow_template_id"] == "standard"

            # First node should be auto-dispatched
            assert data["firstDispatchNodeId"] is not None
            assert data["firstDispatchNodeId"] == data["nodeExecutions"][0]["id"]
            assert data["nodeExecutions"][0]["status"] == "running"
            assert data["run"]["current_node_execution_id"] == data["firstDispatchNodeId"]
            assert data["run"]["active_node_execution_ids"] == [data["firstDispatchNodeId"]]

            # Task should be executing
            task = data["task"]
            assert task["status"] == "executing"

            # NEs keys should match template
            ne_keys = [ne["node_key"] for ne in data["nodeExecutions"]]
            assert ne_keys == ["builder", "reviewer", "deployer", "tester"]

    def test_launch_simple_creates_3_nodes(self, ws_env):
        created = asyncio.run(server.create_task({"title": "Simple Launch"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "simple",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["nodeExecutions"]) == 3
            ne_keys = [ne["node_key"] for ne in data["nodeExecutions"]]
            assert ne_keys == ["builder", "deployer", "tester"]

    def test_launch_pro_creates_9_nodes(self, ws_env):
        created = asyncio.run(server.create_task({"title": "Pro Launch"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "pro",
                "runtime": "openclaw",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["nodeExecutions"]) == 9

    def test_launch_pro_complex_goal_creates_12_nodes(self, ws_env):
        created = asyncio.run(server.create_task({
            "title": "Luxury Deep Launch",
            "description": "做一个介绍奢侈品的八页面网站，页面要非常高级，像苹果官网一样，还有电影感动画转场。",
        }))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "pro",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["nodeExecutions"]) == 12
            assert [ne["node_key"] for ne in data["nodeExecutions"][:6]] == [
                "planner", "analyst", "uidesign", "scribe", "builder1", "builder2"
            ]

    def test_launch_pro_asset_heavy_goal_creates_12_nodes(self, ws_env):
        created = asyncio.run(server.create_task({
            "title": "Asset Deep Launch",
            "description": "做一个奢侈品 lookbook 网站，8 页，包含 hero 插画、lookbook 视觉素材和高质量 asset pack。",
        }))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "pro",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["nodeExecutions"]) == 12
            assert [ne["node_key"] for ne in data["nodeExecutions"][:6]] == [
                "planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder1"
            ]

    def test_launch_pro_asset_heavy_game_creates_12_nodes_with_integrator_builder(self, ws_env):
        created = asyncio.run(server.create_task({
            "title": "Asset Heavy Game Launch",
            "description": "做一个第三人称3d射击游戏，并生成角色模型、武器模型、怪物模型、贴图和asset pack",
        }))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "pro",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["nodeExecutions"]) == 12
            assert [ne["node_key"] for ne in data["nodeExecutions"][5:8]] == [
                "builder1", "builder2", "merger"
            ]

    def test_launch_missing_task_returns_404(self, ws_env):
        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": "nonexistent",
            })
            assert resp.status_code == 404

    def test_launch_unknown_template_returns_400(self, ws_env):
        created = asyncio.run(server.create_task({"title": "Bad Template"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "nonexistent",
            })
            assert resp.status_code == 400

    def test_launch_missing_task_id_returns_400(self, ws_env):
        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={})
            assert resp.status_code == 400

    def test_launch_links_run_to_task(self, ws_env):
        created = asyncio.run(server.create_task({"title": "Link Test"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "standard",
            })
            assert resp.status_code == 200
            run_id = resp.json()["run"]["id"]

            task = task_store.get_task_store().get_task(task_id)
            assert run_id in task.get("run_ids", [])

    def test_launch_with_timeout(self, ws_env):
        created = asyncio.run(server.create_task({"title": "Timeout Launch"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "simple",
                "timeout_seconds": 300,
            })
            assert resp.status_code == 200
            run = resp.json()["run"]
            assert run["timeout_seconds"] == 300

    def test_launch_broadcasts_first_dispatch_to_ws_clients(self, ws_env):
        created = asyncio.run(server.create_task({"title": "Launch Broadcast"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws:
                assert ui_ws.receive_json()["type"] == "connected"

                resp = client.post("/api/runs/launch", json={
                    "task_id": task_id,
                    "template_id": "simple",
                    "runtime": "openclaw",
                })
                assert resp.status_code == 200
                data = resp.json()

                event = ui_ws.receive_json()
                assert event["type"] == "evermind_dispatch_node"
                payload = event["payload"]
                assert payload["runId"] == data["run"]["id"]
                assert payload["taskId"] == task_id
                assert payload["nodeExecutionId"] == data["firstDispatchNodeId"]
                assert payload["launchTriggered"] is True
                assert payload["taskStatus"] == "executing"
                assert payload["runStatus"] == "running"
                assert payload["activeNodeExecutionIds"] == [data["firstDispatchNodeId"]]
