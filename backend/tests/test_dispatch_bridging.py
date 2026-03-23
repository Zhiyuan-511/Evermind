"""
Tests for P1: Run.runtime field and auto-chain logic.
"""
import asyncio
import time
import pytest
import sys
import os
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server
import task_store
from connector_idempotency import connector_idempotency
from task_store import TaskStore, RunStore, NodeExecutionStore, VALID_RUNTIMES


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr("task_store.STORE_DIR", tmp_path)
    monkeypatch.setattr("task_store.TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr("task_store.RUNS_FILE", tmp_path / "runs.json")
    monkeypatch.setattr("task_store.NODE_EXECUTIONS_FILE", tmp_path / "node_executions.json")
    ts = TaskStore()
    ts.load()
    rs = RunStore()
    rs.load()
    ns = NodeExecutionStore()
    ns.load()
    return ts, rs, ns


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
    server._openclaw_dispatch_watchdogs.clear()
    yield
    server.connected_clients.clear()
    server._active_tasks.clear()
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


def _create_openclaw_run_with_nodes(node_keys):
    created_task = asyncio.run(server.create_task({"title": "Dispatch Bridge Task"}))
    task_id = created_task["task"]["id"]
    run_resp = asyncio.run(server.create_run({"task_id": task_id, "runtime": "openclaw"}))
    run_id = run_resp["run"]["id"]
    asyncio.run(server.transition_run(run_id, {"status": "running"}))
    node_ids = []
    for key in node_keys:
        node_resp = asyncio.run(server.create_node_execution({
            "run_id": run_id,
            "node_key": key,
            "node_label": key.title(),
        }))
        node_ids.append(node_resp["nodeExecution"]["id"])
    return task_id, run_id, node_ids


class TestRunRuntimeField:
    """P1-1: Run.runtime field."""

    def test_valid_runtimes(self):
        assert "local" in VALID_RUNTIMES
        assert "openclaw" in VALID_RUNTIMES

    def test_create_run_default_runtime(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1"})
        assert run["runtime"] == "local"

    def test_create_run_openclaw_runtime(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        assert run["runtime"] == "openclaw"

    def test_create_run_invalid_runtime_falls_back(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1", "runtime": "unknown_engine"})
        assert run["runtime"] == "local"

    def test_runtime_persists_in_get(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        fetched = rs.get_run(run["id"])
        assert fetched is not None
        assert fetched["runtime"] == "openclaw"

    def test_runtime_in_list(self, stores):
        _, rs, _ = stores
        rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        rs.create_run({"task_id": "t1", "runtime": "local"})
        runs = rs.list_runs(task_id="t1")
        runtimes = {r["runtime"] for r in runs}
        assert runtimes == {"local", "openclaw"}


class TestAutoChainLogic:
    """P1-2B: Test _auto_chain_next_node logic via store operations."""

    def test_queued_nodes_returns_first_by_creation(self, stores):
        """When multiple nodes are queued, the first created should be returned."""
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        rid = run["id"]

        ne1 = ns.create_node_execution({"run_id": rid, "node_key": "planner"})
        ne2 = ns.create_node_execution({"run_id": rid, "node_key": "builder"})
        ne3 = ns.create_node_execution({"run_id": rid, "node_key": "tester"})

        nodes = ns.list_node_executions(run_id=rid)
        queued = [n for n in nodes if n["status"] == "queued"]
        queued.sort(key=lambda n: float(n.get("created_at", 0) or 0))
        assert len(queued) == 3
        assert queued[0]["id"] == ne1["id"]  # first created

    def test_all_passed_returns_empty_queued(self, stores):
        """When all nodes pass, no queued nodes remain."""
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        rid = run["id"]

        ne1 = ns.create_node_execution({"run_id": rid, "node_key": "planner"})
        ne2 = ns.create_node_execution({"run_id": rid, "node_key": "builder"})

        ns.transition_node(ne1["id"], "running")
        ns.transition_node(ne1["id"], "passed")
        ns.transition_node(ne2["id"], "running")
        ns.transition_node(ne2["id"], "passed")

        nodes = ns.list_node_executions(run_id=rid)
        queued = [n for n in nodes if n["status"] == "queued"]
        assert len(queued) == 0

        statuses = {n["status"] for n in nodes}
        terminal = {"passed", "skipped"}
        assert statuses <= terminal  # all terminal

    def test_after_first_passes_second_is_queued(self, stores):
        """After first node passes, second should be the next queued."""
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        rid = run["id"]

        ne1 = ns.create_node_execution({"run_id": rid, "node_key": "planner"})
        ne2 = ns.create_node_execution({"run_id": rid, "node_key": "builder"})
        ne3 = ns.create_node_execution({"run_id": rid, "node_key": "tester"})

        ns.transition_node(ne1["id"], "running")
        ns.transition_node(ne1["id"], "passed")

        nodes = ns.list_node_executions(run_id=rid)
        queued = [n for n in nodes if n["status"] == "queued"]
        queued.sort(key=lambda n: float(n.get("created_at", 0) or 0))
        assert len(queued) == 2
        assert queued[0]["id"] == ne2["id"]  # builder is next

    def test_mixed_statuses_not_all_terminal(self, stores):
        """When some nodes are running, result is not all-terminal."""
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        rid = run["id"]

        ne1 = ns.create_node_execution({"run_id": rid, "node_key": "planner"})
        ne2 = ns.create_node_execution({"run_id": rid, "node_key": "builder"})

        ns.transition_node(ne1["id"], "running")
        ns.transition_node(ne1["id"], "passed")
        ns.transition_node(ne2["id"], "running")

        nodes = ns.list_node_executions(run_id=rid)
        statuses = {n["status"] for n in nodes}
        terminal = {"passed", "skipped"}
        assert not (statuses <= terminal)  # running is not terminal


class TestDispatchBridgingIntegration:
    def test_passed_node_auto_dispatches_next_queued_node(self, ws_env):
        task_id, run_id, node_ids = _create_openclaw_run_with_nodes(["planner", "builder"])
        first_node_id, second_node_id = node_ids
        now_ms = int(time.time() * 1000)

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                assert ui_ws.receive_json()["type"] == "connected"
                assert runtime_ws.receive_json()["type"] == "connected"

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": f"chain-running:{first_node_id}",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": first_node_id,
                        "status": "running",
                        "timestamp": now_ms,
                    },
                })
                assert ui_ws.receive_json()["type"] == "openclaw_node_update"

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": f"chain-passed:{first_node_id}",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": first_node_id,
                        "status": "passed",
                        "timestamp": now_ms + 1000,
                    },
                })

                forwarded_update = ui_ws.receive_json()
                forwarded_dispatch = ui_ws.receive_json()

                assert forwarded_update["type"] == "openclaw_node_update"
                assert forwarded_dispatch["type"] == "evermind_dispatch_node"
                assert forwarded_dispatch["payload"]["runId"] == run_id
                assert forwarded_dispatch["payload"]["nodeExecutionId"] == second_node_id
                assert forwarded_dispatch["payload"]["nodeKey"] == "builder"
                assert forwarded_dispatch["payload"]["autoChained"] is True
                assert forwarded_dispatch["payload"]["_neVersion"] >= 2
                assert forwarded_dispatch["payload"]["_runVersion"] >= 1
                assert forwarded_dispatch["payload"]["activeNodeExecutionIds"] == [second_node_id]

                second_node = task_store.get_node_execution_store().get_node_execution(second_node_id)
                run = task_store.get_run_store().get_run(run_id)
                task = task_store.get_task_store().get_task(task_id)

                assert second_node["status"] == "running"
                assert run["current_node_execution_id"] == second_node_id
                assert run["active_node_execution_ids"] == [second_node_id]
                assert run["status"] == "running"
                assert task["status"] == "executing"

    def test_last_terminal_node_auto_completes_openclaw_run_and_projects_task(self, ws_env):
        task_id, run_id, node_ids = _create_openclaw_run_with_nodes(["builder"])
        node_id = node_ids[0]
        now_ms = int(time.time() * 1000)

        task_store.get_run_store().update_run(run_id, {
            "summary": "Auto-complete summary",
            "risks": ["Minor residual risk"],
            "total_tokens": 321,
            "total_cost": 1.23,
        })

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                assert ui_ws.receive_json()["type"] == "connected"
                assert runtime_ws.receive_json()["type"] == "connected"

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": f"complete-running:{node_id}",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "status": "running",
                        "timestamp": now_ms,
                    },
                })
                assert ui_ws.receive_json()["type"] == "openclaw_node_update"

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": f"complete-passed:{node_id}",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "status": "passed",
                        "timestamp": now_ms + 1000,
                    },
                })

                forwarded_update = ui_ws.receive_json()
                forwarded_complete = ui_ws.receive_json()

                assert forwarded_update["type"] == "openclaw_node_update"
                assert forwarded_complete["type"] == "openclaw_run_complete"
                assert forwarded_complete["payload"]["runId"] == run_id
                assert forwarded_complete["payload"]["taskId"] == task_id
                assert forwarded_complete["payload"]["autoCompleted"] is True
                assert forwarded_complete["payload"]["summary"] == "Auto-complete summary"
                assert forwarded_complete["payload"]["risks"] == ["Minor residual risk"]
                assert forwarded_complete["payload"]["totalTokens"] == 321
                assert forwarded_complete["payload"]["totalCost"] == 1.23
                assert forwarded_complete["payload"]["_runVersion"] >= 1
                assert forwarded_complete["payload"]["_taskVersion"] >= 1

                run = task_store.get_run_store().get_run(run_id)
                task = task_store.get_task_store().get_task(task_id)
                node = task_store.get_node_execution_store().get_node_execution(node_id)

                assert node["status"] == "passed"
                assert run["status"] == "done"
                assert run["summary"] == "Auto-complete summary"
                assert run["current_node_execution_id"] == ""
                assert run["active_node_execution_ids"] == []
                assert task["status"] == "done"
                assert task["latest_summary"] == "Auto-complete summary"
                assert task["latest_risk"] == "Minor residual risk"
