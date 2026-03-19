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
    BUILT_IN_TEMPLATES,
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
        assert len(tpl["nodes"]) == 7
        keys = [n["key"] for n in tpl["nodes"]]
        assert "analyst" in keys
        assert "builder1" in keys
        assert "builder2" in keys

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

    def test_launch_pro_creates_7_nodes(self, ws_env):
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
            assert len(data["nodeExecutions"]) == 7

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
