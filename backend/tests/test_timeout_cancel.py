"""
Tests for P1-2C: Cancel cascade, timeout watchdog, stale-node endpoint.
"""
import asyncio
import time
import pytest
import sys
import os
from unittest.mock import patch
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server
import task_store
from connector_idempotency import connector_idempotency
from task_store import (
    TaskStore, RunStore, NodeExecutionStore,
    VALID_NODE_STATUSES, VALID_NODE_TRANSITIONS,
)


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
    yield
    server.connected_clients.clear()
    server._active_tasks.clear()
    connector_idempotency.clear()
    task_store._task_store = None
    task_store._report_store = None
    task_store._run_store = None
    task_store._node_execution_store = None
    task_store._artifact_store = None


def _create_run_with_nodes(node_keys, runtime="openclaw"):
    created_task = asyncio.run(server.create_task({"title": "Cancel Test Task"}))
    task_id = created_task["task"]["id"]
    run_resp = asyncio.run(server.create_run({"task_id": task_id, "runtime": runtime}))
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


class TestCancelledNodeStatus:
    """P1-2C Part A: cancelled status in node state machine."""

    def test_cancelled_is_valid_node_status(self):
        assert "cancelled" in VALID_NODE_STATUSES

    def test_cancelled_transitions_from_queued(self):
        assert "cancelled" in VALID_NODE_TRANSITIONS["queued"]

    def test_cancelled_transitions_from_running(self):
        assert "cancelled" in VALID_NODE_TRANSITIONS["running"]

    def test_cancelled_transitions_from_blocked(self):
        assert "cancelled" in VALID_NODE_TRANSITIONS["blocked"]

    def test_cancelled_transitions_from_waiting_approval(self):
        assert "cancelled" in VALID_NODE_TRANSITIONS["waiting_approval"]

    def test_cancelled_is_terminal(self):
        assert VALID_NODE_TRANSITIONS["cancelled"] == []

    def test_transition_node_to_cancelled_sets_ended_at(self, stores):
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1"})
        ne = ns.create_node_execution({"run_id": run["id"], "node_key": "builder"})
        ns.transition_node(ne["id"], "running")
        result = ns.transition_node(ne["id"], "cancelled")
        assert result["success"]
        node_data = result["node_execution"]
        assert node_data["status"] == "cancelled"
        assert node_data["ended_at"] > 0


class TestCancelRunNodes:
    """P1-2C Part A: cancel_run_nodes cascades correctly."""

    def test_cancels_all_non_terminal_nodes(self, stores):
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        rid = run["id"]

        ne1 = ns.create_node_execution({"run_id": rid, "node_key": "planner"})  # queued
        ne2 = ns.create_node_execution({"run_id": rid, "node_key": "builder"})  # will be running
        ne3 = ns.create_node_execution({"run_id": rid, "node_key": "tester"})   # queued

        ns.transition_node(ne2["id"], "running")

        count = ns.cancel_run_nodes(rid)
        assert count == 3  # queued + running + queued

        for ne_id in [ne1["id"], ne2["id"], ne3["id"]]:
            node = ns.get_node_execution(ne_id)
            assert node["status"] == "cancelled"
            assert node["ended_at"] > 0

    def test_skips_already_terminal_nodes(self, stores):
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1", "runtime": "openclaw"})
        rid = run["id"]

        ne1 = ns.create_node_execution({"run_id": rid, "node_key": "planner"})
        ne2 = ns.create_node_execution({"run_id": rid, "node_key": "builder"})

        ns.transition_node(ne1["id"], "running")
        ns.transition_node(ne1["id"], "passed")

        count = ns.cancel_run_nodes(rid)
        assert count == 1  # only ne2 (queued) gets cancelled

        assert ns.get_node_execution(ne1["id"])["status"] == "passed"
        assert ns.get_node_execution(ne2["id"])["status"] == "cancelled"

    def test_returns_zero_for_no_non_terminal(self, stores):
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1"})
        rid = run["id"]

        ne1 = ns.create_node_execution({"run_id": rid, "node_key": "planner"})
        ns.transition_node(ne1["id"], "running")
        ns.transition_node(ne1["id"], "passed")

        count = ns.cancel_run_nodes(rid)
        assert count == 0


class TestCancelCascadeIntegration:
    """P1-2C Part A: cancel run via WS cascades to nodes."""

    def test_cancel_run_cancels_all_nodes(self, ws_env):
        task_id, run_id, node_ids = _create_run_with_nodes(["planner", "builder", "tester"])

        # Transition first node to running
        ns = task_store.get_node_execution_store()
        ns.transition_node(node_ids[0], "running")

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                assert ui_ws.receive_json()["type"] == "connected"
                assert runtime_ws.receive_json()["type"] == "connected"

                runtime_ws.send_json({
                    "type": "evermind_cancel_run",
                    "idempotencyKey": f"cancel:{run_id}",
                    "payload": {"runId": run_id},
                })

                ack = runtime_ws.receive_json()
                assert ack["type"] == "evermind_cancel_run_ack"
                assert ack["payload"]["cancelled"] is True
                assert ack["payload"]["cancelledNodes"] == 3
                assert ack["payload"]["activeNodeExecutionIds"] == []

                # Verify all nodes are cancelled
                for ne_id in node_ids:
                    node = ns.get_node_execution(ne_id)
                    assert node["status"] == "cancelled"

                run = task_store.get_run_store().get_run(run_id)
                assert run["status"] == "cancelled"
                assert run["current_node_execution_id"] == ""
                assert run["active_node_execution_ids"] == []
                task = task_store.get_task_store().get_task(task_id)
                assert task is not None
                assert task["status"] == "backlog"

    def test_rest_cancel_route_cascades_nodes_and_projects_task(self, ws_env):
        task_id, run_id, node_ids = _create_run_with_nodes(["planner", "builder", "tester"])

        ns = task_store.get_node_execution_store()
        ns.transition_node(node_ids[1], "running")

        with TestClient(server.app) as client:
            resp = client.post(f"/api/runs/{run_id}/cancel")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["cancelledNodes"] == 3
            assert data["run"]["status"] == "cancelled"
            assert data["run"]["current_node_execution_id"] == ""
            assert data["run"]["active_node_execution_ids"] == []
            assert data["task"]["status"] == "backlog"

        for ne_id in node_ids:
            node = ns.get_node_execution(ne_id)
            assert node["status"] == "cancelled"

        task = task_store.get_task_store().get_task(task_id)
        assert task is not None
        assert task["status"] == "backlog"

    def test_rest_node_transition_updates_run_activity(self, ws_env):
        _task_id, run_id, node_ids = _create_run_with_nodes(["planner"])
        node_id = node_ids[0]

        with TestClient(server.app) as client:
            running_resp = client.post(f"/api/node-executions/{node_id}/transition", json={"status": "running"})
            assert running_resp.status_code == 200
            assert running_resp.json()["success"] is True

            run = task_store.get_run_store().get_run(run_id)
            assert run["current_node_execution_id"] == node_id
            assert run["active_node_execution_ids"] == [node_id]

            passed_resp = client.post(f"/api/node-executions/{node_id}/transition", json={"status": "passed"})
            assert passed_resp.status_code == 200
            assert passed_resp.json()["success"] is True

            run = task_store.get_run_store().get_run(run_id)
            assert run["current_node_execution_id"] == ""
            assert run["active_node_execution_ids"] == []


class TestTimeoutConstants:
    """P1-2C Part B: timeout fields and constants."""

    def test_run_has_timeout_seconds_field(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1"})
        assert "timeout_seconds" in run
        assert run["timeout_seconds"] == 0

    def test_node_has_timeout_seconds_field(self, stores):
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1"})
        ne = ns.create_node_execution({"run_id": run["id"], "node_key": "planner"})
        assert "timeout_seconds" in ne
        assert ne["timeout_seconds"] == 0

    def test_run_timeout_persists_on_create_and_update(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1", "timeout_seconds": 123})
        assert run["timeout_seconds"] == 123

        fetched = rs.get_run(run["id"])
        assert fetched is not None
        assert fetched["timeout_seconds"] == 123

        updated = rs.update_run(run["id"], {"timeout_seconds": 456})
        assert updated is not None
        assert updated["timeout_seconds"] == 456

        fetched_again = rs.get_run(run["id"])
        assert fetched_again is not None
        assert fetched_again["timeout_seconds"] == 456

    def test_node_timeout_persists_on_create_and_update(self, stores):
        _, rs, ns = stores
        run = rs.create_run({"task_id": "t1"})
        ne = ns.create_node_execution({
            "run_id": run["id"],
            "node_key": "planner",
            "timeout_seconds": 42,
        })
        assert ne["timeout_seconds"] == 42

        fetched = ns.get_node_execution(ne["id"])
        assert fetched is not None
        assert fetched["timeout_seconds"] == 42

        updated = ns.update_node_execution(ne["id"], {"timeout_seconds": 84})
        assert updated is not None
        assert updated["timeout_seconds"] == 84

        fetched_again = ns.get_node_execution(ne["id"])
        assert fetched_again is not None
        assert fetched_again["timeout_seconds"] == 84

    def test_default_timeout_constants_exist(self):
        assert server.DEFAULT_NODE_TIMEOUT_S == 600
        assert server.DEFAULT_RUN_TIMEOUT_S == 3600
        assert server.WATCHDOG_INTERVAL_S == 30


class TestStaleNodesEndpoint:
    """P1-2C Part C: stale nodes endpoint."""

    def test_returns_stale_running_nodes(self, ws_env):
        task_id, run_id, node_ids = _create_run_with_nodes(["planner", "builder"])
        ns = task_store.get_node_execution_store()
        ns.transition_node(node_ids[0], "running")

        # Backdate the updated_at to make it stale
        node_obj = ns._nodes[node_ids[0]]
        node_obj.updated_at = time.time() - 120  # 2 minutes ago
        node_obj.started_at = time.time() - 120
        ns.save()

        with TestClient(server.app) as client:
            resp = client.get(f"/api/runs/{run_id}/stale-nodes?stale_threshold_s=60")
            assert resp.status_code == 200
            data = resp.json()
            assert data["runId"] == run_id
            assert data["runtime"] == "openclaw"
            assert len(data["staleNodes"]) == 1
            assert data["staleNodes"][0]["id"] == node_ids[0]
            assert data["staleNodes"][0]["node_key"] == "planner"
            assert data["staleNodes"][0]["elapsed_since_update"] > 60

    def test_returns_empty_for_fresh_running_nodes(self, ws_env):
        task_id, run_id, node_ids = _create_run_with_nodes(["planner"])
        ns = task_store.get_node_execution_store()
        ns.transition_node(node_ids[0], "running")
        # Node just started — not stale

        with TestClient(server.app) as client:
            resp = client.get(f"/api/runs/{run_id}/stale-nodes?stale_threshold_s=60")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["staleNodes"]) == 0

    def test_returns_404_for_missing_run(self, ws_env):
        with TestClient(server.app) as client:
            resp = client.get("/api/runs/nonexistent_run/stale-nodes")
            assert resp.status_code == 404
