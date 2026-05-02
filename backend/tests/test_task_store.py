"""Tests for task_store module — CRUD, transitions, persistence, linking."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import task_store
from task_store import (
    TaskStore, ReportStore, TaskRecord, VALID_TRANSITIONS, VALID_STATUSES,
)


class TestTaskRecord(unittest.TestCase):
    def test_from_dict_creates_valid_record(self):
        data = {"id": "t1", "title": "Test Task", "status": "backlog", "priority": "high"}
        record = TaskRecord.from_dict(data)
        self.assertEqual(record.id, "t1")
        self.assertEqual(record.title, "Test Task")
        self.assertEqual(record.status, "backlog")
        self.assertEqual(record.priority, "high")

    def test_from_dict_ignores_unknown_fields(self):
        data = {"id": "t2", "title": "Test", "unknown_field": "ignored"}
        record = TaskRecord.from_dict(data)
        self.assertEqual(record.id, "t2")
        self.assertFalse(hasattr(record, "unknown_field"))

    def test_to_dict_roundtrip(self):
        record = TaskRecord(id="t3", title="Roundtrip", status="planned")
        d = record.to_dict()
        restored = TaskRecord.from_dict(d)
        self.assertEqual(restored.id, "t3")
        self.assertEqual(restored.title, "Roundtrip")
        self.assertEqual(restored.status, "planned")


class TestTaskStoreCRUD(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tasks_file = Path(self.tmpdir.name) / "tasks.json"
        # Patch the file paths
        self.patcher_dir = patch.object(task_store, "STORE_DIR", Path(self.tmpdir.name))
        self.patcher_file = patch.object(task_store, "TASKS_FILE", self.tasks_file)
        self.patcher_dir.start()
        self.patcher_file.start()
        self.store = TaskStore()

    def tearDown(self):
        self.patcher_dir.stop()
        self.patcher_file.stop()
        self.tmpdir.cleanup()

    def test_create_and_list(self):
        result = self.store.create_task({"title": "My Task", "priority": "high"})
        self.assertEqual(result["title"], "My Task")
        self.assertEqual(result["priority"], "high")
        self.assertEqual(result["status"], "backlog")
        self.assertTrue(result["id"])

        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "My Task")

    def test_get_task(self):
        created = self.store.create_task({"title": "Find Me"})
        found = self.store.get_task(created["id"])
        self.assertIsNotNone(found)
        self.assertEqual(found["title"], "Find Me")

    def test_get_nonexistent_returns_none(self):
        self.assertIsNone(self.store.get_task("nonexistent"))

    def test_update_task(self):
        created = self.store.create_task({"title": "Original"})
        updated = self.store.update_task(created["id"], {"title": "Updated", "priority": "urgent"})
        self.assertEqual(updated["title"], "Updated")
        self.assertEqual(updated["priority"], "urgent")

    def test_update_nonexistent_returns_none(self):
        self.assertIsNone(self.store.update_task("nope", {"title": "X"}))

    def test_delete_task(self):
        created = self.store.create_task({"title": "Delete Me"})
        self.assertTrue(self.store.delete_task(created["id"]))
        self.assertIsNone(self.store.get_task(created["id"]))
        self.assertFalse(self.store.delete_task(created["id"]))

    def test_create_validates_status(self):
        result = self.store.create_task({"title": "Bad Status", "status": "invalid"})
        self.assertEqual(result["status"], "backlog")

    def test_create_validates_priority(self):
        result = self.store.create_task({"title": "Bad Priority", "priority": "critical"})
        self.assertEqual(result["priority"], "medium")

    def test_create_validates_mode(self):
        result = self.store.create_task({"title": "Bad Mode", "mode": "turbo"})
        self.assertEqual(result["mode"], "standard")

    def test_create_clamps_progress(self):
        result = self.store.create_task({"title": "Over Progress", "progress": 150})
        self.assertEqual(result["progress"], 100)

    def test_create_normalizes_lists_and_selfcheck(self):
        result = self.store.create_task({
            "title": "Normalize Me",
            "run_ids": ["run_1", "run_1", "", None],
            "related_files": ["/tmp/a.html", "/tmp/a.html", 123],
            "review_issues": ["fix css", "", "fix css"],
            "selfcheck_items": [
                {"name": "Preview loads", "passed": True, "detail": "ok"},
                {"name": "", "detail": ""},
            ],
        })
        self.assertEqual(result["run_ids"], ["run_1"])
        self.assertEqual(result["related_files"], ["/tmp/a.html", "123"])
        self.assertEqual(result["review_issues"], ["fix css"])
        self.assertEqual(len(result["selfcheck_items"]), 1)

    def test_update_coerces_progress_and_lists(self):
        created = self.store.create_task({"title": "Original"})
        updated = self.store.update_task(created["id"], {
            "progress": "88",
            "mode": "turbo",
            "priority": "critical",
            "review_issues": ["a", "a", ""],
            "selfcheck_items": [{"name": "Lint", "passed": 1, "detail": "clean"}],
        })
        self.assertEqual(updated["progress"], 88)
        self.assertEqual(updated["mode"], "standard")
        self.assertEqual(updated["priority"], "medium")
        self.assertEqual(updated["review_issues"], ["a"])
        self.assertEqual(updated["selfcheck_items"][0]["name"], "Lint")


class TestTaskStoreTransitions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tasks_file = Path(self.tmpdir.name) / "tasks.json"
        self.patcher_dir = patch.object(task_store, "STORE_DIR", Path(self.tmpdir.name))
        self.patcher_file = patch.object(task_store, "TASKS_FILE", self.tasks_file)
        self.patcher_dir.start()
        self.patcher_file.start()
        self.store = TaskStore()

    def tearDown(self):
        self.patcher_dir.stop()
        self.patcher_file.stop()
        self.tmpdir.cleanup()

    def test_valid_transition_backlog_to_planned(self):
        task = self.store.create_task({"title": "Transition Test"})
        result = self.store.transition_task(task["id"], "planned")
        self.assertTrue(result["success"])
        self.assertEqual(result["task"]["status"], "planned")
        self.assertEqual(result["task"]["progress"], 10)

    def test_valid_transition_backlog_to_executing(self):
        task = self.store.create_task({"title": "Direct Start"})
        result = self.store.transition_task(task["id"], "executing")
        self.assertTrue(result["success"])
        self.assertEqual(result["task"]["status"], "executing")
        self.assertEqual(result["task"]["progress"], 30)

    def test_invalid_transition_backlog_to_done(self):
        task = self.store.create_task({"title": "Skip Test"})
        result = self.store.transition_task(task["id"], "done")
        self.assertFalse(result["success"])
        self.assertIn("Cannot transition", result["error"])

    def test_full_happy_path(self):
        """Test complete lifecycle: backlog → planned → executing → review → selfcheck → done."""
        task = self.store.create_task({"title": "Full Path"})
        path = ["planned", "executing", "review", "selfcheck", "done"]
        for status in path:
            result = self.store.transition_task(task["id"], status)
            self.assertTrue(result["success"], f"Failed transition to {status}: {result.get('error')}")
            self.assertEqual(result["task"]["status"], status)

    def test_review_rejection_back_to_executing(self):
        task = self.store.create_task({"title": "Review Reject"})
        self.store.transition_task(task["id"], "planned")
        self.store.transition_task(task["id"], "executing")
        self.store.transition_task(task["id"], "review")
        result = self.store.transition_task(task["id"], "executing")
        self.assertTrue(result["success"])
        self.assertEqual(result["task"]["status"], "executing")

    def test_selfcheck_fail_back_to_executing(self):
        task = self.store.create_task({"title": "Selfcheck Fail"})
        for s in ["planned", "executing", "review", "selfcheck"]:
            self.store.transition_task(task["id"], s)
        result = self.store.transition_task(task["id"], "executing")
        self.assertTrue(result["success"])

    def test_transition_nonexistent_task(self):
        result = self.store.transition_task("nope", "planned")
        self.assertFalse(result["success"])

    def test_transition_invalid_status(self):
        task = self.store.create_task({"title": "Invalid"})
        result = self.store.transition_task(task["id"], "mythical")
        self.assertFalse(result["success"])

    def test_all_transitions_are_documented(self):
        """Verify every status has a transition entry."""
        for status in VALID_STATUSES:
            self.assertIn(status, VALID_TRANSITIONS)


class TestTaskStorePersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tasks_file = Path(self.tmpdir.name) / "tasks.json"
        self.patcher_dir = patch.object(task_store, "STORE_DIR", Path(self.tmpdir.name))
        self.patcher_file = patch.object(task_store, "TASKS_FILE", self.tasks_file)
        self.patcher_dir.start()
        self.patcher_file.start()

    def tearDown(self):
        self.patcher_dir.stop()
        self.patcher_file.stop()
        self.tmpdir.cleanup()

    def test_persistence_roundtrip(self):
        store1 = TaskStore()
        store1.create_task({"title": "Persist Me"})
        store1.create_task({"title": "Persist Me Too"})

        # New store should load from disk
        store2 = TaskStore()
        store2.load()
        tasks = store2.list_tasks()
        self.assertEqual(len(tasks), 2)
        titles = {t["title"] for t in tasks}
        self.assertIn("Persist Me", titles)
        self.assertIn("Persist Me Too", titles)

    def test_empty_file_loads_gracefully(self):
        self.tasks_file.write_text("", encoding="utf-8")
        store = TaskStore()
        store.load()
        self.assertEqual(len(store.list_tasks()), 0)

    def test_corrupt_file_loads_gracefully(self):
        self.tasks_file.write_text("{not valid json", encoding="utf-8")
        store = TaskStore()
        store.load()
        self.assertEqual(len(store.list_tasks()), 0)


class TestAtomicJsonWrite(unittest.TestCase):
    def test_write_json_file_cleans_up_tempfile_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as td:
            store_dir = Path(td)
            target = store_dir / "tasks.json"
            target.write_text('[{"id":"stable"}]', encoding="utf-8")

            with patch.object(task_store, "STORE_DIR", store_dir):
                with patch("task_store.os.replace", side_effect=OSError("replace failed")):
                    ok = task_store._write_json_file(target, [{"id": "t1"}])

            self.assertFalse(ok)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                [{"id": "stable"}],
            )
            self.assertEqual(list(store_dir.glob(".tasks_*.tmp")), [])


class TestTaskStoreLinking(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tasks_file = Path(self.tmpdir.name) / "tasks.json"
        self.patcher_dir = patch.object(task_store, "STORE_DIR", Path(self.tmpdir.name))
        self.patcher_file = patch.object(task_store, "TASKS_FILE", self.tasks_file)
        self.patcher_dir.start()
        self.patcher_file.start()
        self.store = TaskStore()

    def tearDown(self):
        self.patcher_dir.stop()
        self.patcher_file.stop()
        self.tmpdir.cleanup()

    def test_link_run(self):
        task = self.store.create_task({"title": "Link Test"})
        result = self.store.link_run(
            task["id"], "run_001",
            summary="Built a landing page",
            risk="No tests",
            files=["/tmp/evermind_output/index.html"],
        )
        self.assertIsNotNone(result)
        self.assertIn("run_001", result["run_ids"])
        self.assertEqual(result["latest_summary"], "Built a landing page")
        self.assertIn("/tmp/evermind_output/index.html", result["related_files"])
        self.assertEqual(result["version"], 1)

    def test_link_run_deduplicates(self):
        task = self.store.create_task({"title": "Dedup"})
        self.store.link_run(task["id"], "run_001", files=["/a.html"])
        self.store.link_run(task["id"], "run_001", files=["/a.html", "/b.css"])
        result = self.store.get_task(task["id"])
        self.assertEqual(result["run_ids"].count("run_001"), 1)
        self.assertEqual(result["related_files"].count("/a.html"), 1)

    def test_link_run_noop_does_not_bump_version(self):
        task = self.store.create_task({"title": "Stable Link"})
        first = self.store.link_run(task["id"], "run_001", summary="same", risk="risk", files=["/a.html"])
        self.assertIsNotNone(first)
        version_before = first["version"]

        second = self.store.link_run(task["id"], "run_001", summary="same", risk="risk", files=["/a.html"])
        self.assertIsNotNone(second)
        self.assertEqual(second["version"], version_before)

    def test_link_run_nonexistent_task(self):
        self.assertIsNone(self.store.link_run("nope", "run_x"))

    def test_update_appends_run_ids(self):
        task = self.store.create_task({"title": "Append Test"})
        self.store.update_task(task["id"], {"run_ids": ["run_1"]})
        self.store.update_task(task["id"], {"run_ids": ["run_2"]})
        result = self.store.get_task(task["id"])
        self.assertIn("run_1", result["run_ids"])
        self.assertIn("run_2", result["run_ids"])


class TestReportStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.reports_file = Path(self.tmpdir.name) / "reports.json"
        self.patcher_dir = patch.object(task_store, "STORE_DIR", Path(self.tmpdir.name))
        self.patcher_file = patch.object(task_store, "REPORTS_FILE", self.reports_file)
        self.patcher_dir.start()
        self.patcher_file.start()
        self.store = ReportStore()

    def tearDown(self):
        self.patcher_dir.stop()
        self.patcher_file.stop()
        self.tmpdir.cleanup()

    def test_save_and_list_reports(self):
        report = self.store.save_report({
            "id": "r1", "task_id": "t1", "goal": "Build page",
            "success": True, "difficulty": "standard",
        })
        self.assertEqual(report["id"], "r1")
        reports = self.store.list_reports()
        self.assertEqual(len(reports), 1)

    def test_filter_by_task_id(self):
        self.store.save_report({"id": "r1", "task_id": "t1", "goal": "A"})
        self.store.save_report({"id": "r2", "task_id": "t2", "goal": "B"})
        self.store.save_report({"id": "r3", "task_id": "t1", "goal": "C"})
        t1_reports = self.store.list_reports(task_id="t1")
        self.assertEqual(len(t1_reports), 2)

    def test_get_report(self):
        self.store.save_report({"id": "r5", "goal": "Find me"})
        found = self.store.get_report("r5")
        self.assertIsNotNone(found)
        self.assertEqual(found["goal"], "Find me")

    def test_delete_report(self):
        self.store.save_report({"id": "r6", "goal": "Delete me"})
        self.assertTrue(self.store.delete_report("r6"))
        self.assertIsNone(self.store.get_report("r6"))

    def test_auto_generates_id(self):
        report = self.store.save_report({"goal": "No ID given"})
        self.assertTrue(report["id"])
        self.assertTrue(report["id"].startswith("run_"))

    def test_persistence_roundtrip(self):
        self.store.save_report({"id": "r10", "goal": "Persist"})
        store2 = ReportStore()
        store2.load()
        self.assertEqual(len(store2.list_reports()), 1)


if __name__ == "__main__":
    unittest.main()
