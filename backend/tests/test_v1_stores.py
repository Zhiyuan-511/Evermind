"""Tests for V1 stores: RunStore, NodeExecutionStore, ArtifactStore."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import task_store
from task_store import (
    RunStore, NodeExecutionStore, ArtifactStore,
    RunRecord, NodeExecutionRecord, ArtifactRecord,
    ReviewDecisionRecord, ValidationResultRecord,
    VALID_RUN_STATUSES, VALID_RUN_TRANSITIONS,
    VALID_NODE_STATUSES, VALID_NODE_TRANSITIONS,
    VALID_ARTIFACT_TYPES, MAX_NODE_RETRY_COUNT,
)


class _TempDirMixin:
    """Sets up temp dir and patches store file paths."""
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.patchers = [
            patch.object(task_store, "STORE_DIR", self.tmp_path),
            patch.object(task_store, "RUNS_FILE", self.tmp_path / "runs.json"),
            patch.object(task_store, "NODE_EXECUTIONS_FILE", self.tmp_path / "node_executions.json"),
            patch.object(task_store, "ARTIFACTS_FILE", self.tmp_path / "artifacts.json"),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.tmpdir.cleanup()


# ── RunRecord dataclass ──

class TestRunRecord(unittest.TestCase):
    def test_roundtrip(self):
        r = RunRecord(id="r1", task_id="t1", status="queued")
        d = r.to_dict()
        r2 = RunRecord.from_dict(d)
        self.assertEqual(r2.id, "r1")
        self.assertEqual(r2.task_id, "t1")

    def test_ignores_unknown_fields(self):
        r = RunRecord.from_dict({"id": "r2", "unknown_key": "ignored"})
        self.assertEqual(r.id, "r2")

    def test_defaults(self):
        r = RunRecord()
        self.assertEqual(r.status, "queued")
        self.assertEqual(r.trigger_source, "ui")
        self.assertEqual(r.node_execution_ids, [])


# ── RunStore CRUD ──

class TestRunStoreCRUD(_TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = RunStore()

    def test_create_and_list(self):
        run = self.store.create_run({"task_id": "t1", "trigger_source": "ui"})
        self.assertTrue(run["id"].startswith("run_"))
        self.assertEqual(run["task_id"], "t1")
        self.assertEqual(run["status"], "queued")
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_get_run(self):
        run = self.store.create_run({"task_id": "t1"})
        found = self.store.get_run(run["id"])
        self.assertIsNotNone(found)
        self.assertEqual(found["task_id"], "t1")

    def test_get_nonexistent(self):
        self.assertIsNone(self.store.get_run("nope"))

    def test_list_filter_by_task(self):
        self.store.create_run({"task_id": "t1"})
        self.store.create_run({"task_id": "t2"})
        self.store.create_run({"task_id": "t1"})
        self.assertEqual(len(self.store.list_runs(task_id="t1")), 2)

    def test_create_validates_trigger_source(self):
        run = self.store.create_run({"task_id": "t1", "trigger_source": "invalid"})
        self.assertEqual(run["trigger_source"], "ui")

    def test_delete_run(self):
        run = self.store.create_run({"task_id": "t1"})
        self.assertTrue(self.store.delete_run(run["id"]))
        self.assertIsNone(self.store.get_run(run["id"]))
        self.assertFalse(self.store.delete_run(run["id"]))

    def test_update_run(self):
        run = self.store.create_run({"task_id": "t1"})
        updated = self.store.update_run(run["id"], {
            "summary": "Completed successfully",
            "total_tokens": 15000,
            "total_cost": 0.25,
            "risks": ["Warning: no tests"],
        })
        self.assertEqual(updated["summary"], "Completed successfully")
        self.assertEqual(updated["total_tokens"], 15000)
        self.assertEqual(updated["total_cost"], 0.25)
        self.assertEqual(updated["risks"], ["Warning: no tests"])

    def test_update_nonexistent(self):
        self.assertIsNone(self.store.update_run("nope", {"summary": "x"}))

    def test_update_appends_node_execution_ids(self):
        run = self.store.create_run({"task_id": "t1"})
        self.store.update_run(run["id"], {"node_execution_ids": ["ne1"]})
        self.store.update_run(run["id"], {"node_execution_ids": ["ne2", "ne1"]})
        result = self.store.get_run(run["id"])
        self.assertEqual(result["node_execution_ids"], ["ne1", "ne2"])


# ── RunStore transitions ──

class TestRunStoreTransitions(_TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = RunStore()

    def test_all_statuses_have_transitions(self):
        for status in VALID_RUN_STATUSES:
            self.assertIn(status, VALID_RUN_TRANSITIONS)

    def test_happy_path(self):
        run = self.store.create_run({"task_id": "t1"})
        result = self.store.transition_run(run["id"], "running")
        self.assertTrue(result["success"])
        self.assertEqual(result["run"]["status"], "running")
        self.assertGreater(result["run"]["started_at"], 0)

    def test_full_lifecycle(self):
        run = self.store.create_run({"task_id": "t1"})
        for status in ["running", "waiting_review", "running", "done"]:
            result = self.store.transition_run(run["id"], status)
            self.assertTrue(result["success"], f"Failed at {status}: {result.get('error')}")

    def test_invalid_transition(self):
        run = self.store.create_run({"task_id": "t1"})
        result = self.store.transition_run(run["id"], "done")  # queued → done invalid
        self.assertFalse(result["success"])
        self.assertIn("Cannot transition", result["error"])

    def test_nonexistent_run(self):
        result = self.store.transition_run("nope", "running")
        self.assertFalse(result["success"])

    def test_invalid_status(self):
        run = self.store.create_run({"task_id": "t1"})
        result = self.store.transition_run(run["id"], "mythical")
        self.assertFalse(result["success"])

    def test_done_is_terminal(self):
        run = self.store.create_run({"task_id": "t1"})
        self.store.transition_run(run["id"], "running")
        self.store.transition_run(run["id"], "done")
        result = self.store.transition_run(run["id"], "running")
        self.assertFalse(result["success"])

    def test_ended_at_set_on_terminal(self):
        run = self.store.create_run({"task_id": "t1"})
        self.store.transition_run(run["id"], "running")
        result = self.store.transition_run(run["id"], "failed")
        self.assertGreater(result["run"]["ended_at"], 0)

    def test_cancel_from_running(self):
        run = self.store.create_run({"task_id": "t1"})
        self.store.transition_run(run["id"], "running")
        result = self.store.transition_run(run["id"], "cancelled")
        self.assertTrue(result["success"])

    def test_requeue_clears_terminal_timestamps(self):
        run = self.store.create_run({"task_id": "t1"})
        self.store.transition_run(run["id"], "running")
        self.store.transition_run(run["id"], "failed")
        result = self.store.transition_run(run["id"], "queued")
        self.assertTrue(result["success"])
        self.assertEqual(result["run"]["started_at"], 0.0)
        self.assertEqual(result["run"]["ended_at"], 0.0)

    def test_waiting_selfcheck_to_running(self):
        run = self.store.create_run({"task_id": "t1"})
        self.store.transition_run(run["id"], "running")
        self.store.transition_run(run["id"], "waiting_selfcheck")
        result = self.store.transition_run(run["id"], "running")
        self.assertTrue(result["success"])


# ── RunStore persistence ──

class TestRunStorePersistence(_TempDirMixin, unittest.TestCase):
    def test_roundtrip(self):
        s1 = RunStore()
        s1.create_run({"task_id": "t1"})
        s1.create_run({"task_id": "t2"})
        s2 = RunStore()
        s2.load()
        self.assertEqual(len(s2.list_runs()), 2)


# ── NodeExecutionRecord dataclass ──

class TestNodeExecutionRecord(unittest.TestCase):
    def test_roundtrip(self):
        n = NodeExecutionRecord(id="ne1", run_id="r1", node_key="builder", retried_from_id="ne0")
        d = n.to_dict()
        n2 = NodeExecutionRecord.from_dict(d)
        self.assertEqual(n2.id, "ne1")
        self.assertEqual(n2.node_key, "builder")
        self.assertEqual(n2.retried_from_id, "ne0")

    def test_defaults(self):
        n = NodeExecutionRecord()
        self.assertEqual(n.status, "queued")
        self.assertEqual(n.artifact_ids, [])
        self.assertEqual(n.retried_from_id, "")


# ── NodeExecutionStore CRUD ──

class TestNodeExecutionStoreCRUD(_TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = NodeExecutionStore()

    def test_create_and_list(self):
        ne = self.store.create_node_execution({
            "run_id": "r1", "node_key": "builder", "node_label": "构建者",
            "assigned_model": "gpt-5.4", "retried_from_id": "nodeexec_prev",
        })
        self.assertTrue(ne["id"].startswith("nodeexec_"))
        self.assertEqual(ne["node_key"], "builder")
        self.assertEqual(ne["assigned_model"], "gpt-5.4")
        self.assertEqual(ne["retried_from_id"], "nodeexec_prev")
        self.assertEqual(len(self.store.list_node_executions()), 1)

    def test_list_filter_by_run(self):
        self.store.create_node_execution({"run_id": "r1", "node_key": "planner"})
        self.store.create_node_execution({"run_id": "r2", "node_key": "builder"})
        self.store.create_node_execution({"run_id": "r1", "node_key": "reviewer"})
        self.assertEqual(len(self.store.list_node_executions(run_id="r1")), 2)

    def test_get_and_update(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "tester"})
        updated = self.store.update_node_execution(ne["id"], {
            "output_summary": "All 42 tests pass",
            "tokens_used": 8000,
            "cost": 0.05,
            "retried_from_id": "nodeexec_root",
        })
        self.assertEqual(updated["output_summary"], "All 42 tests pass")
        self.assertEqual(updated["tokens_used"], 8000)
        self.assertEqual(updated["retried_from_id"], "nodeexec_root")

    def test_retry_limit_constant_is_positive(self):
        self.assertGreaterEqual(MAX_NODE_RETRY_COUNT, 1)

    def test_update_appends_artifact_ids(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "builder"})
        self.store.update_node_execution(ne["id"], {"artifact_ids": ["a1"]})
        self.store.update_node_execution(ne["id"], {"artifact_ids": ["a2", "a1"]})
        result = self.store.get_node_execution(ne["id"])
        self.assertEqual(result["artifact_ids"], ["a1", "a2"])

    def test_update_nonexistent(self):
        self.assertIsNone(self.store.update_node_execution("nope", {}))


# ── NodeExecutionStore transitions ──

class TestNodeExecutionStoreTransitions(_TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = NodeExecutionStore()

    def test_all_statuses_have_transitions(self):
        for status in VALID_NODE_STATUSES:
            self.assertIn(status, VALID_NODE_TRANSITIONS)

    def test_happy_path(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "builder"})
        result = self.store.transition_node(ne["id"], "running")
        self.assertTrue(result["success"])
        self.assertGreater(result["node_execution"]["started_at"], 0)
        result = self.store.transition_node(ne["id"], "passed")
        self.assertTrue(result["success"])
        self.assertGreater(result["node_execution"]["ended_at"], 0)

    def test_passed_is_terminal(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "builder"})
        self.store.transition_node(ne["id"], "running")
        self.store.transition_node(ne["id"], "passed")
        result = self.store.transition_node(ne["id"], "running")
        self.assertFalse(result["success"])

    def test_failed_is_terminal(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "builder"})
        self.store.transition_node(ne["id"], "running")
        self.store.transition_node(ne["id"], "failed")
        result = self.store.transition_node(ne["id"], "running")
        self.assertFalse(result["success"])

    def test_waiting_approval_to_passed(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "reviewer"})
        self.store.transition_node(ne["id"], "running")
        self.store.transition_node(ne["id"], "waiting_approval")
        result = self.store.transition_node(ne["id"], "passed")
        self.assertTrue(result["success"])

    def test_blocked_to_running(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "debugger"})
        self.store.transition_node(ne["id"], "running")
        self.store.transition_node(ne["id"], "blocked")
        result = self.store.transition_node(ne["id"], "running")
        self.assertTrue(result["success"])

    def test_queued_to_skipped(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "deployer"})
        result = self.store.transition_node(ne["id"], "skipped")
        self.assertTrue(result["success"])
        self.assertGreater(result["node_execution"]["ended_at"], 0)

    def test_nonexistent(self):
        result = self.store.transition_node("nope", "running")
        self.assertFalse(result["success"])

    def test_invalid_status(self):
        ne = self.store.create_node_execution({"run_id": "r1", "node_key": "x"})
        result = self.store.transition_node(ne["id"], "mythical")
        self.assertFalse(result["success"])


# ── NodeExecutionStore persistence ──

class TestNodeExecutionStorePersistence(_TempDirMixin, unittest.TestCase):
    def test_roundtrip(self):
        s1 = NodeExecutionStore()
        s1.create_node_execution({"run_id": "r1", "node_key": "builder"})
        s2 = NodeExecutionStore()
        s2.load()
        self.assertEqual(len(s2.list_node_executions()), 1)


# ── ArtifactRecord dataclass ──

class TestArtifactRecord(unittest.TestCase):
    def test_roundtrip(self):
        a = ArtifactRecord(id="a1", run_id="r1", artifact_type="report", title="Summary")
        d = a.to_dict()
        a2 = ArtifactRecord.from_dict(d)
        self.assertEqual(a2.title, "Summary")

    def test_defaults(self):
        a = ArtifactRecord()
        self.assertEqual(a.artifact_type, "report")
        self.assertEqual(a.metadata, {})


# ── ArtifactStore CRUD ──

class TestArtifactStoreCRUD(_TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = ArtifactStore()

    def test_save_and_list(self):
        a = self.store.save_artifact({
            "run_id": "r1", "node_execution_id": "ne1",
            "type": "diff_summary", "title": "Diff",
            "path": "/tmp/diff.md",
        })
        self.assertTrue(a["id"].startswith("artifact_"))
        self.assertEqual(a["artifact_type"], "diff_summary")
        self.assertEqual(len(self.store.list_artifacts()), 1)

    def test_filter_by_run(self):
        self.store.save_artifact({"run_id": "r1", "title": "A"})
        self.store.save_artifact({"run_id": "r2", "title": "B"})
        self.assertEqual(len(self.store.list_artifacts(run_id="r1")), 1)

    def test_filter_by_node(self):
        self.store.save_artifact({"run_id": "r1", "node_execution_id": "ne1", "title": "A"})
        self.store.save_artifact({"run_id": "r1", "node_execution_id": "ne2", "title": "B"})
        self.assertEqual(len(self.store.list_artifacts(node_execution_id="ne1")), 1)

    def test_get_artifact(self):
        a = self.store.save_artifact({"title": "Find me"})
        found = self.store.get_artifact(a["id"])
        self.assertEqual(found["title"], "Find me")

    def test_delete_artifact(self):
        a = self.store.save_artifact({"title": "Delete me"})
        self.assertTrue(self.store.delete_artifact(a["id"]))
        self.assertIsNone(self.store.get_artifact(a["id"]))
        self.assertFalse(self.store.delete_artifact(a["id"]))

    def test_validates_artifact_type(self):
        a = self.store.save_artifact({"type": "invalid_type", "title": "x"})
        self.assertEqual(a["artifact_type"], "report")  # default

    def test_content_stored(self):
        a = self.store.save_artifact({
            "title": "Inline",
            "content": "Hello world",
            "metadata": {"mimeType": "text/plain"},
        })
        self.assertEqual(a["content"], "Hello world")
        self.assertEqual(a["metadata"]["mimeType"], "text/plain")


# ── ArtifactStore persistence ──

class TestArtifactStorePersistence(_TempDirMixin, unittest.TestCase):
    def test_roundtrip(self):
        s1 = ArtifactStore()
        s1.save_artifact({"title": "Persist me"})
        s2 = ArtifactStore()
        s2.load()
        self.assertEqual(len(s2.list_artifacts()), 1)


# ── ReviewDecisionRecord + ValidationResultRecord ──

class TestReviewAndValidationRecords(unittest.TestCase):
    def test_review_roundtrip(self):
        r = ReviewDecisionRecord(
            id="rev1", run_id="r1", node_execution_id="ne1",
            decision="reject", issues=["Missing tests"],
            remaining_risks=["No coverage"], next_action="route_to_debugger",
        )
        d = r.to_dict()
        r2 = ReviewDecisionRecord.from_dict(d)
        self.assertEqual(r2.decision, "reject")
        self.assertEqual(r2.issues, ["Missing tests"])

    def test_validation_roundtrip(self):
        v = ValidationResultRecord(
            id="val1", run_id="r1", node_execution_id="ne1",
            summary_status="failed",
            checklist=[{"name": "pytest", "status": "passed"}, {"name": "build", "status": "failed"}],
            summary="Build broken",
        )
        d = v.to_dict()
        v2 = ValidationResultRecord.from_dict(d)
        self.assertEqual(v2.summary_status, "failed")
        self.assertEqual(len(v2.checklist), 2)


if __name__ == "__main__":
    unittest.main()
