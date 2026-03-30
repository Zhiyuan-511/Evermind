"""
Tests for P0-3: Version auto-increment in RunStore and NodeExecutionStore.
Validates version monotonic increase and inclusion in returned dicts.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from task_store import TaskStore, RunStore, NodeExecutionStore


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


class TestTaskVersioning:
    def test_create_sets_version_zero(self, stores):
        ts, _, _ = stores
        task = ts.create_task({"title": "Test"})
        assert "version" in task
        assert task["version"] == 0

    def test_update_increments_version(self, stores):
        ts, _, _ = stores
        task = ts.create_task({"title": "Test"})
        assert task["version"] == 0
        updated = ts.update_task(task["id"], {"title": "Updated"})
        assert updated["version"] == 1
        updated2 = ts.update_task(task["id"], {"title": "Updated again"})
        assert updated2["version"] == 2

    def test_transition_increments_version(self, stores):
        ts, _, _ = stores
        task = ts.create_task({"title": "Test"})
        result = ts.transition_task(task["id"], "planned")
        assert result["task"]["version"] == 1
        result2 = ts.transition_task(task["id"], "executing")
        assert result2["task"]["version"] == 2

    def test_projection_increments_version(self, stores):
        ts, _, _ = stores
        task = ts.create_task({"title": "Test"})
        ts.transition_task(task["id"], "planned")
        ts.transition_task(task["id"], "executing")
        v_before = ts.get_task(task["id"])["version"]
        result = ts.project_task_from_run(task["id"], summary="new summary")
        assert result["version"] == v_before + 1

    def test_projection_noop_no_version_bump(self, stores):
        ts, _, _ = stores
        task = ts.create_task({"title": "Test"})
        ts.transition_task(task["id"], "planned")
        ts.transition_task(task["id"], "executing")
        ts.project_task_from_run(task["id"], summary="first summary")
        v1 = ts.get_task(task["id"])["version"]
        # Same summary → no change → no version bump
        ts.project_task_from_run(task["id"], summary="first summary")
        v2 = ts.get_task(task["id"])["version"]
        assert v2 == v1

    def test_link_run_increments_version_on_change(self, stores):
        ts, _, _ = stores
        task = ts.create_task({"title": "Test"})
        updated = ts.link_run(task["id"], "run_1", summary="done")
        assert updated["version"] == 1

    def test_link_run_noop_does_not_bump_version(self, stores):
        ts, _, _ = stores
        task = ts.create_task({"title": "Test"})
        ts.link_run(task["id"], "run_1", summary="done")
        v1 = ts.get_task(task["id"])["version"]
        ts.link_run(task["id"], "run_1", summary="done")
        v2 = ts.get_task(task["id"])["version"]
        assert v2 == v1


class TestRunVersioning:
    def test_create_sets_version_one(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1"})
        assert run["version"] == 1

    def test_transition_increments_version(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1"})
        result = rs.transition_run(run["id"], "running")
        assert result["run"]["version"] == 2
        result2 = rs.transition_run(run["id"], "done")
        assert result2["run"]["version"] == 3

    def test_update_increments_version(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1"})
        updated = rs.update_run(run["id"], {"summary": "test"})
        assert updated["version"] == 2

    def test_monotonic_increase(self, stores):
        _, rs, _ = stores
        run = rs.create_run({"task_id": "t1"})
        versions = [run["version"]]
        for _ in range(5):
            updated = rs.update_run(run["id"], {"summary": f"update {_}"})
            versions.append(updated["version"])
        # Each version strictly greater than previous
        for i in range(1, len(versions)):
            assert versions[i] > versions[i - 1]


class TestNodeExecutionVersioning:
    def test_create_sets_version_one(self, stores):
        _, _, ns = stores
        ne = ns.create_node_execution({"run_id": "r1", "node_key": "builder"})
        assert ne["version"] == 1

    def test_transition_increments_version(self, stores):
        _, _, ns = stores
        ne = ns.create_node_execution({"run_id": "r1", "node_key": "builder"})
        result = ns.transition_node(ne["id"], "running")
        assert result["node_execution"]["version"] == 2
        result2 = ns.transition_node(ne["id"], "passed")
        assert result2["node_execution"]["version"] == 3

    def test_update_increments_version(self, stores):
        _, _, ns = stores
        ne = ns.create_node_execution({"run_id": "r1", "node_key": "builder"})
        updated = ns.update_node_execution(ne["id"], {"output_summary": "done"})
        assert updated["version"] == 2

    def test_version_in_to_dict(self, stores):
        _, _, ns = stores
        ne = ns.create_node_execution({"run_id": "r1", "node_key": "builder"})
        d = ns.get_node_execution(ne["id"])
        assert "version" in d
        assert d["version"] == 1
