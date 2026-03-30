"""
Tests for P3: DAG-based node chaining + depends_on_keys + progress emit.
"""
import asyncio
import pytest
import sys
import os
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server
import task_store
from connector_idempotency import connector_idempotency
from workflow_templates import get_template


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
    monkeypatch.setattr(server, "_acquire_backend_runtime_lock", lambda: None)
    monkeypatch.setattr(server, "_release_backend_runtime_lock", lambda: None)
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


class TestDependsOnKeys:
    """P3-A: depends_on_keys field in NodeExecutionRecord."""

    def test_create_node_with_depends_on_keys(self, ws_env):
        run = task_store.get_run_store().create_run({"task_id": "t1"})
        ne = task_store.get_node_execution_store().create_node_execution({
            "run_id": run["id"],
            "node_key": "reviewer",
            "depends_on_keys": ["builder1", "builder2"],
        })
        assert ne["depends_on_keys"] == ["builder1", "builder2"]

    def test_get_preserves_depends_on_keys(self, ws_env):
        run = task_store.get_run_store().create_run({"task_id": "t1"})
        ne = task_store.get_node_execution_store().create_node_execution({
            "run_id": run["id"],
            "node_key": "tester",
            "depends_on_keys": ["deployer"],
        })
        fetched = task_store.get_node_execution_store().get_node_execution(ne["id"])
        assert fetched["depends_on_keys"] == ["deployer"]

    def test_update_depends_on_keys(self, ws_env):
        run = task_store.get_run_store().create_run({"task_id": "t1"})
        ne = task_store.get_node_execution_store().create_node_execution({
            "run_id": run["id"],
            "node_key": "tester",
            "depends_on_keys": [],
        })
        updated = task_store.get_node_execution_store().update_node_execution(
            ne["id"], {"depends_on_keys": ["builder"]}
        )
        assert updated["depends_on_keys"] == ["builder"]

    def test_empty_depends_on_keys_by_default(self, ws_env):
        run = task_store.get_run_store().create_run({"task_id": "t1"})
        ne = task_store.get_node_execution_store().create_node_execution({
            "run_id": run["id"],
            "node_key": "builder",
        })
        assert ne["depends_on_keys"] == []


class TestDAGChaining:
    """P3-B: DAG-aware _auto_chain_next_node()."""

    def _create_run_with_nodes(self, ws_env, template_id="pro"):
        """Helper: create a run with NEs from a template."""
        run = task_store.get_run_store().create_run({"task_id": "t1", "runtime": "openclaw"})
        run_id = run["id"]
        tpl = get_template(template_id)
        nes_store = task_store.get_node_execution_store()
        nes = []
        for node_def in tpl["nodes"]:
            ne = nes_store.create_node_execution({
                "run_id": run_id,
                "node_key": node_def["key"],
                "node_label": node_def["label"],
                "depends_on_keys": node_def.get("depends_on", []),
            })
            nes.append(ne)
        return run_id, nes

    def test_root_nodes_dispatched_first(self, ws_env):
        """Pro template: only analyst (root) should be ready initially."""
        run_id, nes = self._create_run_with_nodes(ws_env, "pro")
        result = server._auto_chain_next_node(run_id)
        assert isinstance(result, list)
        assert len(result) == 1
        # Should be the analyst (only root node)
        analyst_ne = [ne for ne in nes if ne["node_key"] == "analyst"][0]
        assert result[0] == analyst_ne["id"]

    def test_parallel_dispatch_after_root(self, ws_env):
        """Pro: analyst passed → builder1 AND builder2 should be ready (parallel)."""
        run_id, nes = self._create_run_with_nodes(ws_env, "pro")
        nes_store = task_store.get_node_execution_store()
        analyst_ne = [ne for ne in nes if ne["node_key"] == "analyst"][0]
        nes_store.transition_node(analyst_ne["id"], "running")
        nes_store.transition_node(analyst_ne["id"], "passed")

        result = server._auto_chain_next_node(run_id)
        assert isinstance(result, list)
        assert len(result) == 2
        ready_keys = set()
        for ne_id in result:
            ne = nes_store.get_node_execution(ne_id)
            ready_keys.add(ne["node_key"])
        assert ready_keys == {"builder1", "builder2"}

    def test_join_node_waits_for_all_deps(self, ws_env):
        """Pro: reviewer depends on builder1+builder2. Only ready when both passed."""
        run_id, nes = self._create_run_with_nodes(ws_env, "pro")
        nes_store = task_store.get_node_execution_store()

        # Pass analyst
        analyst_ne = [ne for ne in nes if ne["node_key"] == "analyst"][0]
        nes_store.transition_node(analyst_ne["id"], "running")
        nes_store.transition_node(analyst_ne["id"], "passed")

        # Pass only builder1
        b1 = [ne for ne in nes if ne["node_key"] == "builder1"][0]
        nes_store.transition_node(b1["id"], "running")
        nes_store.transition_node(b1["id"], "passed")

        # builder2 still queued → reviewer/deployer should NOT be ready
        result = server._auto_chain_next_node(run_id)
        assert isinstance(result, list)
        ready_keys = {nes_store.get_node_execution(ne_id)["node_key"] for ne_id in result}
        assert "reviewer" not in ready_keys
        assert "deployer" not in ready_keys
        # builder2 should now be dispatched (its dep analyst is passed)
        assert "builder2" in ready_keys

    def test_join_node_ready_when_all_deps_passed(self, ws_env):
        """Pro: both builders passed → reviewer + deployer ready."""
        run_id, nes = self._create_run_with_nodes(ws_env, "pro")
        nes_store = task_store.get_node_execution_store()

        for key in ["analyst", "builder1", "builder2"]:
            ne = [n for n in nes if n["node_key"] == key][0]
            nes_store.transition_node(ne["id"], "running")
            nes_store.transition_node(ne["id"], "passed")

        result = server._auto_chain_next_node(run_id)
        assert isinstance(result, list)
        ready_keys = {nes_store.get_node_execution(ne_id)["node_key"] for ne_id in result}
        assert "reviewer" in ready_keys
        assert "deployer" in ready_keys

    def test_all_done_returns_sentinel(self, ws_env):
        """All nodes passed → __ALL_DONE__."""
        run_id, nes = self._create_run_with_nodes(ws_env, "simple")
        nes_store = task_store.get_node_execution_store()
        for ne in nes:
            nes_store.transition_node(ne["id"], "running")
            nes_store.transition_node(ne["id"], "passed")

        result = server._auto_chain_next_node(run_id)
        assert result == "__ALL_DONE__"

    def test_failed_dep_terminalizes_downstream_without_dispatch(self, ws_env):
        """If a dep failed and no safe preview exists, downstream nodes should not dispatch.

        Instead, they should be terminalized so the run can finish cleanly without hanging."""
        run_id, nes = self._create_run_with_nodes(ws_env, "simple")
        nes_store = task_store.get_node_execution_store()
        builder = [ne for ne in nes if ne["node_key"] == "builder"][0]
        nes_store.transition_node(builder["id"], "running")
        nes_store.transition_node(builder["id"], "failed")

        result = server._auto_chain_next_node(run_id)
        assert result == "__ALL_DONE__"
        deployer = [ne for ne in nes_store.list_node_executions(run_id=run_id) if ne["node_key"] == "deployer"][0]
        tester = [ne for ne in nes_store.list_node_executions(run_id=run_id) if ne["node_key"] == "tester"][0]
        assert deployer["status"] == "cancelled"
        assert tester["status"] == "cancelled"

    def test_failed_builder_still_blocks_join_nodes_even_when_preview_exists(self, ws_env, tmp_path):
        """A failed builder must block reviewer/deployer even if some preview artifact already exists."""
        run_id, nes = self._create_run_with_nodes(ws_env, "pro")
        nes_store = task_store.get_node_execution_store()

        out_dir = tmp_path / "preview"
        out_dir.mkdir(parents=True, exist_ok=True)
        preview = out_dir / "index.html"
        preview.write_text(
            "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><style>body{margin:0}main{display:grid}@media(max-width:700px){main{display:block}}</style></head><body><header>Ready</header><main><section>Preview</section><section>More</section><footer>Done</footer></main><script>1</script></body></html>",
            encoding="utf-8",
        )

        original_output = server.OUTPUT_DIR
        try:
            server.OUTPUT_DIR = Path(out_dir)
            for key in ["analyst", "builder1"]:
                ne = [n for n in nes if n["node_key"] == key][0]
                nes_store.transition_node(ne["id"], "running")
                nes_store.transition_node(ne["id"], "passed")
            builder2 = [n for n in nes if n["node_key"] == "builder2"][0]
            nes_store.transition_node(builder2["id"], "running")
            nes_store.transition_node(builder2["id"], "failed")

            result = server._auto_chain_next_node(run_id)
        finally:
            server.OUTPUT_DIR = original_output

        assert result == "__ALL_DONE__"
        reviewer = [ne for ne in nes_store.list_node_executions(run_id=run_id) if ne["node_key"] == "reviewer"][0]
        deployer = [ne for ne in nes_store.list_node_executions(run_id=run_id) if ne["node_key"] == "deployer"][0]
        assert reviewer["status"] == "cancelled"
        assert deployer["status"] == "cancelled"

    def test_standard_template_linear_chain(self, ws_env):
        """Standard: builder (root) → reviewer+deployer → tester."""
        run_id, nes = self._create_run_with_nodes(ws_env, "standard")
        nes_store = task_store.get_node_execution_store()

        # Initially only builder should be ready
        result = server._auto_chain_next_node(run_id)
        assert isinstance(result, list)
        assert len(result) == 1
        builder = [ne for ne in nes if ne["node_key"] == "builder"][0]
        assert result[0] == builder["id"]

        # Pass builder → reviewer + deployer ready (both depend on builder)
        nes_store.transition_node(builder["id"], "running")
        nes_store.transition_node(builder["id"], "passed")

        result = server._auto_chain_next_node(run_id)
        assert isinstance(result, list)
        ready_keys = {nes_store.get_node_execution(ne_id)["node_key"] for ne_id in result}
        assert ready_keys == {"reviewer", "deployer"}


class TestLaunchWithDAG:
    """P3-B: launch endpoint with DAG-aware dispatch."""

    def test_launch_pro_dispatches_analyst_only(self, ws_env):
        """Pro template has analyst as only root → only analyst dispatched."""
        created = asyncio.run(server.create_task({"title": "DAG Pro"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "pro",
                "runtime": "openclaw",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"]

            # Only analyst should be dispatched
            dispatched = data.get("dispatchedNodeIds", [])
            assert len(dispatched) == 1
            analyst_ne = [ne for ne in data["nodeExecutions"] if ne["node_key"] == "analyst"][0]
            assert dispatched[0] == analyst_ne["id"]
            assert analyst_ne["status"] == "running"

            # All others should still be queued
            for ne in data["nodeExecutions"]:
                if ne["node_key"] != "analyst":
                    assert ne["status"] == "queued", f"{ne['node_key']} should be queued"

    def test_launch_stores_depends_on_keys(self, ws_env):
        """NEs created from template should have depends_on_keys persisted."""
        created = asyncio.run(server.create_task({"title": "Deps Check"}))
        task_id = created["task"]["id"]

        with TestClient(server.app) as client:
            resp = client.post("/api/runs/launch", json={
                "task_id": task_id,
                "template_id": "pro",
                "runtime": "openclaw",
            })
            data = resp.json()
            nes = data["nodeExecutions"]
            # Verify deps match template
            analyst = [ne for ne in nes if ne["node_key"] == "analyst"][0]
            assert analyst["depends_on_keys"] == []
            b1 = [ne for ne in nes if ne["node_key"] == "builder1"][0]
            assert b1["depends_on_keys"] == ["analyst"]
            reviewer = [ne for ne in nes if ne["node_key"] == "reviewer"][0]
            assert set(reviewer["depends_on_keys"]) == {"builder1", "builder2"}


class TestProgressBroadcast:
    """P3-C: openclaw_node_progress WS handler."""

    def test_progress_broadcast_round_trip(self, ws_env):
        """Send openclaw_node_progress via sender WS, verify listener WS receives it."""
        run = task_store.get_run_store().create_run({"task_id": "t1", "runtime": "openclaw"})
        ne = task_store.get_node_execution_store().create_node_execution({
            "run_id": run["id"], "node_key": "builder",
        })
        task_store.get_node_execution_store().transition_node(ne["id"], "running")

        with TestClient(server.app) as client:
            # Two connections: sender (connector) and listener (UI)
            with client.websocket_connect("/ws") as listener_ws:
                listener_ws.receive_json()  # connected
                with client.websocket_connect("/ws") as sender_ws:
                    sender_ws.receive_json()  # connected

                    sender_ws.send_json({
                        "type": "openclaw_node_progress",
                        "payload": {
                            "runId": run["id"],
                            "nodeExecutionId": ne["id"],
                            "progress": 42,
                            "partialOutput": "Building components...",
                            "phase": "building",
                        },
                    })

                    # Listener should receive the broadcast
                    evt = listener_ws.receive_json()
                    assert evt["type"] == "openclaw_node_progress"
                    p = evt["payload"]
                    assert p["nodeExecutionId"] == ne["id"]
                    assert p["progress"] == 42
                    assert p["partialOutput"] == "Building components..."
                    assert p["phase"] == "building"
                    assert p["timestamp"] > 0
                    assert "_neVersion" in p

    def test_progress_updates_output_summary(self, ws_env):
        """partialOutput should persist into NE output_summary."""
        run = task_store.get_run_store().create_run({"task_id": "t1"})
        ne = task_store.get_node_execution_store().create_node_execution({
            "run_id": run["id"], "node_key": "builder",
        })
        task_store.get_node_execution_store().transition_node(ne["id"], "running")

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as listener_ws:
                listener_ws.receive_json()
                with client.websocket_connect("/ws") as sender_ws:
                    sender_ws.receive_json()

                    sender_ws.send_json({
                        "type": "openclaw_node_progress",
                        "payload": {
                            "runId": run["id"],
                            "nodeExecutionId": ne["id"],
                            "partialOutput": "Compiling TypeScript...",
                        },
                    })
                    listener_ws.receive_json()  # consume broadcast

        ne_after = task_store.get_node_execution_store().get_node_execution(ne["id"])
        assert ne_after["output_summary"] == "Compiling TypeScript..."

    def test_progress_for_missing_node_is_ignored(self, ws_env):
        """Unknown nodeExecutionId should not be broadcast to listeners."""
        run = task_store.get_run_store().create_run({"task_id": "t1", "runtime": "openclaw"})
        ne = task_store.get_node_execution_store().create_node_execution({
            "run_id": run["id"], "node_key": "builder",
        })
        task_store.get_node_execution_store().transition_node(ne["id"], "running")

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as listener_ws:
                listener_ws.receive_json()
                with client.websocket_connect("/ws") as sender_ws:
                    sender_ws.receive_json()

                    sender_ws.send_json({
                        "type": "openclaw_node_progress",
                        "payload": {
                            "runId": run["id"],
                            "nodeExecutionId": "missing_ne",
                            "progress": 13,
                        },
                    })
                    sender_ws.send_json({
                        "type": "openclaw_node_progress",
                        "payload": {
                            "runId": run["id"],
                            "nodeExecutionId": ne["id"],
                            "progress": 64,
                        },
                    })

                    evt = listener_ws.receive_json()
                    assert evt["type"] == "openclaw_node_progress"
                    assert evt["payload"]["nodeExecutionId"] == ne["id"]
                    assert evt["payload"]["progress"] == 64
