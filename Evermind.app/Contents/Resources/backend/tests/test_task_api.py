import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import agent_skills
import server
import task_store


class TaskApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tasks_file = Path(self.tmpdir.name) / "tasks.json"
        self.reports_file = Path(self.tmpdir.name) / "reports.json"
        self.runs_file = Path(self.tmpdir.name) / "runs.json"
        self.node_executions_file = Path(self.tmpdir.name) / "node_executions.json"
        self.artifacts_file = Path(self.tmpdir.name) / "artifacts.json"
        self.patcher_dir = patch.object(task_store, "STORE_DIR", Path(self.tmpdir.name))
        self.patcher_tasks = patch.object(task_store, "TASKS_FILE", self.tasks_file)
        self.patcher_reports = patch.object(task_store, "REPORTS_FILE", self.reports_file)
        self.patcher_runs = patch.object(task_store, "RUNS_FILE", self.runs_file)
        self.patcher_node_executions = patch.object(task_store, "NODE_EXECUTIONS_FILE", self.node_executions_file)
        self.patcher_artifacts = patch.object(task_store, "ARTIFACTS_FILE", self.artifacts_file)
        self.patcher_dir.start()
        self.patcher_tasks.start()
        self.patcher_reports.start()
        self.patcher_runs.start()
        self.patcher_node_executions.start()
        self.patcher_artifacts.start()
        task_store._task_store = None
        task_store._report_store = None
        task_store._run_store = None
        task_store._node_execution_store = None
        task_store._artifact_store = None

    def tearDown(self):
        task_store._task_store = None
        task_store._report_store = None
        task_store._run_store = None
        task_store._node_execution_store = None
        task_store._artifact_store = None
        self.patcher_artifacts.stop()
        self.patcher_node_executions.stop()
        self.patcher_runs.stop()
        self.patcher_reports.stop()
        self.patcher_tasks.stop()
        self.patcher_dir.stop()
        self.tmpdir.cleanup()

    def _decode_error(self, response):
        self.assertEqual(response.media_type, "application/json")
        return json.loads(response.body.decode("utf-8"))

    def test_task_endpoints_return_camel_case(self):
        created = asyncio.run(server.create_task({
            "title": "API Task",
            "runIds": ["run_1", "run_1"],
            "relatedFiles": ["/tmp/index.html"],
            "reviewIssues": ["fix css"],
            "selfcheckItems": [{"name": "Preview", "passed": True, "detail": "ok"}],
        }))

        task = created["task"]
        self.assertIn("createdAt", task)
        self.assertIn("updatedAt", task)
        self.assertIn("version", task)
        self.assertEqual(task["version"], 0)
        self.assertGreater(task["createdAt"], 10_000_000_000)
        self.assertEqual(task["runIds"], ["run_1"])
        self.assertEqual(task["relatedFiles"], ["/tmp/index.html"])
        self.assertEqual(task["reviewIssues"], ["fix css"])
        self.assertEqual(task["selfcheckItems"][0]["name"], "Preview")
        self.assertNotIn("created_at", task)

        listed = asyncio.run(server.list_tasks())
        self.assertEqual(listed["tasks"][0]["id"], task["id"])
        self.assertIn("runIds", listed["tasks"][0])
        self.assertIn("version", listed["tasks"][0])

    def test_list_tasks_hydrates_live_progress_from_active_run_nodes(self):
        created = asyncio.run(server.create_task({"title": "Progress Task"}))
        task_id = created["task"]["id"]
        ts = task_store.get_task_store()
        ts.transition_task(task_id, "planned")
        ts.transition_task(task_id, "executing")

        run = task_store.get_run_store().create_run({
            "task_id": task_id,
            "status": "running",
        })
        ne_store = task_store.get_node_execution_store()
        ne1 = ne_store.create_node_execution({"run_id": run["id"], "node_key": "analyst", "status": "passed"})
        ne2 = ne_store.create_node_execution({"run_id": run["id"], "node_key": "builder", "status": "running"})
        ne3 = ne_store.create_node_execution({"run_id": run["id"], "node_key": "tester", "status": "queued"})
        task_store.get_run_store().update_run(run["id"], {
            "node_execution_ids": [ne1["id"], ne2["id"], ne3["id"]],
            "current_node_execution_id": ne2["id"],
            "active_node_execution_ids": [ne2["id"]],
        })

        listed = asyncio.run(server.list_tasks())
        task = next(item for item in listed["tasks"] if item["id"] == task_id)
        self.assertGreater(task["progress"], 30)

    def test_update_task_accepts_camel_case_fields(self):
        created = asyncio.run(server.create_task({"title": "Review Me"}))
        task_id = created["task"]["id"]

        updated = asyncio.run(server.update_task(task_id, {
            "reviewVerdict": "approved",
            "reviewIssues": ["missing tests"],
            "latestSummary": "Ready to merge",
            "selfcheckItems": [{"name": "Smoke", "passed": True, "detail": "ok"}],
        }))

        task = updated["task"]
        self.assertEqual(task["reviewVerdict"], "approved")
        self.assertEqual(task["reviewIssues"], ["missing tests"])
        self.assertEqual(task["latestSummary"], "Ready to merge")
        self.assertEqual(task["selfcheckItems"][0]["name"], "Smoke")

        persisted = task_store.get_task_store().get_task(task_id)
        self.assertEqual(persisted["review_verdict"], "approved")
        self.assertEqual(persisted["review_issues"], ["missing tests"])
        self.assertEqual(persisted["latest_summary"], "Ready to merge")

    def test_report_endpoints_accept_and_return_camel_case(self):
        created = asyncio.run(server.create_task({"title": "Run Owner"}))
        task_id = created["task"]["id"]
        created_run = asyncio.run(server.create_run({"id": "run_api_1", "task_id": task_id}))
        run_id = created_run["run"]["id"]

        saved = asyncio.run(server.save_report({
            "id": "report_api_1",
            "taskId": task_id,
            "runId": run_id,
            "createdAt": 1_710_000_000_000,
            "goal": "Build landing page",
            "difficulty": "pro",
            "success": True,
            "totalSubtasks": 2,
            "completed": 2,
            "failed": 0,
            "totalRetries": 1,
            "durationSeconds": 12.5,
            "previewUrl": "/preview/task/index.html",
            "subtasks": [{
                "id": "1",
                "agent": "builder",
                "status": "completed",
                "retries": 0,
                "task": "Build page",
                "outputPreview": "done",
                "filesCreated": ["/tmp/index.html"],
                "workSummary": ["Built page"],
                "startedAt": 1_710_000_000_000,
                "endedAt": 1_710_000_010_000,
            }],
        }))

        report = saved["report"]
        self.assertEqual(report["taskId"], task_id)
        self.assertEqual(report["runId"], run_id)
        self.assertEqual(report["totalSubtasks"], 2)
        self.assertEqual(report["totalRetries"], 1)
        self.assertEqual(report["previewUrl"], "/preview/task/index.html")
        self.assertEqual(report["subtasks"][0]["outputPreview"], "done")
        self.assertEqual(report["subtasks"][0]["filesCreated"], ["/tmp/index.html"])
        self.assertEqual(report["subtasks"][0]["workSummary"], ["Built page"])
        self.assertGreater(report["createdAt"], 10_000_000_000)

        listed = asyncio.run(server.list_reports(taskId=task_id))
        self.assertEqual(len(listed["reports"]), 1)
        self.assertEqual(listed["reports"][0]["id"], "report_api_1")

        linked_task = task_store.get_task_store().get_task(task_id)
        self.assertIn(run_id, linked_task["run_ids"])
        self.assertNotIn("report_api_1", linked_task["run_ids"])
        self.assertIn("/tmp/index.html", linked_task["related_files"])

        loaded_task = asyncio.run(server.get_task(task_id))
        self.assertEqual(loaded_task["task"]["reports"][0]["taskId"], task_id)
        self.assertEqual(loaded_task["task"]["reports"][0]["runId"], run_id)

    def test_report_without_run_id_does_not_corrupt_task_run_ids(self):
        created = asyncio.run(server.create_task({"title": "Report Safety"}))
        task_id = created["task"]["id"]

        saved = asyncio.run(server.save_report({
            "id": "report_only_1",
            "taskId": task_id,
            "goal": "Summarize findings",
            "difficulty": "standard",
            "success": True,
        }))

        self.assertEqual(saved["report"]["taskId"], task_id)
        self.assertEqual(saved["report"]["id"], "report_only_1")
        self.assertEqual(saved["report"]["runId"], "")
        linked_task = task_store.get_task_store().get_task(task_id)
        self.assertEqual(linked_task["run_ids"], [])

    def test_board_summary_uses_node_label_for_active_nodes(self):
        created_task = asyncio.run(server.create_task({"title": "Board Summary"}))
        task_id = created_task["task"]["id"]
        run = asyncio.run(server.create_run({"task_id": task_id}))
        run_id = run["run"]["id"]

        ne_one = asyncio.run(server.create_node_execution({
            "run_id": run_id,
            "node_key": "builder",
            "node_label": "Builder Alpha",
        }))
        ne_two = asyncio.run(server.create_node_execution({
            "run_id": run_id,
            "node_key": "tester",
            "node_label": "QA Beta",
        }))

        task_store.get_run_store().transition_run(run_id, "running")
        task_store.get_run_store().update_run(run_id, {
            "active_node_execution_ids": [
                ne_one["nodeExecution"]["id"],
                ne_two["nodeExecution"]["id"],
            ],
        })
        task_store.get_node_execution_store().transition_node(ne_one["nodeExecution"]["id"], "running")
        task_store.get_node_execution_store().transition_node(ne_two["nodeExecution"]["id"], "running")

        summary = asyncio.run(server.board_summary())
        task_payload = next(task for task in summary["tasks"] if task["id"] == task_id)
        self.assertEqual(task_payload["latestRun"]["id"], run_id)
        self.assertEqual(task_payload["activeNodeLabel"], "Builder Alpha")
        self.assertEqual(task_payload["activeNodeLabels"], ["Builder Alpha", "QA Beta"])

    def test_create_run_broadcasts_run_created_and_task_updated(self):
        created_task = asyncio.run(server.create_task({"title": "Broadcast Parent"}))
        task_id = created_task["task"]["id"]

        with patch.object(server, "_broadcast_ws_event", new=AsyncMock()) as mock_broadcast:
            created_run = asyncio.run(server.create_run({"task_id": task_id}))

        self.assertTrue(created_run["run"]["id"])
        event_types = [call.args[0]["type"] for call in mock_broadcast.await_args_list]
        self.assertIn("run_created", event_types)
        self.assertIn("task_updated", event_types)

    def test_create_run_requires_existing_task_and_rejects_duplicate_id(self):
        missing = asyncio.run(server.create_run({"task_id": "missing-task"}))
        self.assertEqual(missing.status_code, 404)
        self.assertIn("not found", self._decode_error(missing)["error"].lower())

        created_task = asyncio.run(server.create_task({"title": "Run Parent"}))
        task_id = created_task["task"]["id"]

        created_run = asyncio.run(server.create_run({"id": "run_api_1", "task_id": task_id}))
        self.assertEqual(created_run["run"]["id"], "run_api_1")

        duplicate = asyncio.run(server.create_run({"id": "run_api_1", "task_id": task_id}))
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("already exists", self._decode_error(duplicate)["error"])

        task = task_store.get_task_store().get_task(task_id)
        self.assertEqual(task["run_ids"], ["run_api_1"])

    def test_create_run_auto_transitions_pre_execution_task_to_executing(self):
        cases = (
            ("backlog", 0),
            ("planned", 10),
        )
        for initial_status, initial_progress in cases:
            with self.subTest(initial_status=initial_status):
                created_task = asyncio.run(server.create_task({
                    "title": f"Run Parent {initial_status}",
                    "status": initial_status,
                    "progress": initial_progress,
                }))
                task_id = created_task["task"]["id"]

                created_run = asyncio.run(server.create_run({"task_id": task_id}))
                stored_task = task_store.get_task_store().get_task(task_id)

                self.assertEqual(created_run["run"]["task_id"], task_id)
                self.assertEqual(stored_task["status"], "executing")
                self.assertEqual(stored_task["progress"], 30)
                self.assertEqual(stored_task["run_ids"], [created_run["run"]["id"]])

    def test_create_run_does_not_regress_later_task_statuses(self):
        cases = (
            ("review", 60),
            ("selfcheck", 80),
            ("done", 100),
        )
        for initial_status, initial_progress in cases:
            with self.subTest(initial_status=initial_status):
                created_task = asyncio.run(server.create_task({
                    "title": f"Run Parent {initial_status}",
                    "status": initial_status,
                    "progress": initial_progress,
                }))
                task_id = created_task["task"]["id"]

                created_run = asyncio.run(server.create_run({"task_id": task_id}))
                stored_task = task_store.get_task_store().get_task(task_id)

                self.assertEqual(created_run["run"]["task_id"], task_id)
                self.assertEqual(stored_task["status"], initial_status)
                self.assertEqual(stored_task["progress"], initial_progress)
                self.assertEqual(stored_task["run_ids"], [created_run["run"]["id"]])

    def test_create_node_execution_requires_existing_run_and_node_key(self):
        missing_run = asyncio.run(server.create_node_execution({"run_id": "missing-run", "node_key": "builder"}))
        self.assertEqual(missing_run.status_code, 404)

        created_task = asyncio.run(server.create_task({"title": "Node Parent"}))
        task_id = created_task["task"]["id"]
        run = asyncio.run(server.create_run({"id": "run_for_node", "task_id": task_id}))

        missing_key = asyncio.run(server.create_node_execution({"run_id": run["run"]["id"]}))
        self.assertEqual(missing_key.status_code, 400)
        self.assertIn("node_key is required", self._decode_error(missing_key)["error"])

        created_node = asyncio.run(server.create_node_execution({
            "id": "nodeexec_api_1",
            "run_id": run["run"]["id"],
            "node_key": "builder",
        }))
        self.assertEqual(created_node["nodeExecution"]["id"], "nodeexec_api_1")

        duplicate = asyncio.run(server.create_node_execution({
            "id": "nodeexec_api_1",
            "run_id": run["run"]["id"],
            "node_key": "builder",
        }))
        self.assertEqual(duplicate.status_code, 409)

        stored_run = task_store.get_run_store().get_run(run["run"]["id"])
        self.assertEqual(stored_run["node_execution_ids"], ["nodeexec_api_1"])
        self.assertEqual(stored_run["current_node_execution_id"], "nodeexec_api_1")

    def test_retry_node_execution_requires_failed_or_blocked_status(self):
        created_task = asyncio.run(server.create_task({"title": "Retry Guard"}))
        task_id = created_task["task"]["id"]
        run = asyncio.run(server.create_run({"id": "run_retry_guard", "task_id": task_id}))
        node = asyncio.run(server.create_node_execution({
            "id": "nodeexec_retry_guard",
            "run_id": run["run"]["id"],
            "node_key": "builder",
            "status": "queued",
        }))

        response = asyncio.run(server.retry_node_execution(node["nodeExecution"]["id"]))
        self.assertEqual(response.status_code, 400)
        self.assertIn("can only retry failed or blocked nodes", self._decode_error(response)["error"].lower())

    def test_retry_node_execution_creates_new_attempt_and_updates_run_pointer(self):
        created_task = asyncio.run(server.create_task({"title": "Retry Parent"}))
        task_id = created_task["task"]["id"]
        run = asyncio.run(server.create_run({"id": "run_retry_ok", "task_id": task_id}))
        old_node = asyncio.run(server.create_node_execution({
            "id": "nodeexec_failed_1",
            "run_id": run["run"]["id"],
            "node_key": "builder",
            "node_label": "Builder",
            "assigned_model": "gpt-5",
            "assigned_provider": "openai",
            "status": "failed",
            "retry_count": 2,
        }))

        retried = asyncio.run(server.retry_node_execution(old_node["nodeExecution"]["id"]))
        new_node = retried["nodeExecution"]
        stored_run = task_store.get_run_store().get_run(run["run"]["id"])

        self.assertEqual(retried["retriedFrom"], old_node["nodeExecution"]["id"])
        self.assertNotEqual(new_node["id"], old_node["nodeExecution"]["id"])
        self.assertEqual(new_node["run_id"], run["run"]["id"])
        self.assertEqual(new_node["node_key"], "builder")
        self.assertEqual(new_node["node_label"], "Builder")
        self.assertEqual(new_node["retried_from_id"], old_node["nodeExecution"]["id"])
        self.assertEqual(new_node["assigned_model"], "gpt-5")
        self.assertEqual(new_node["assigned_provider"], "openai")
        self.assertEqual(new_node["retry_count"], 3)
        self.assertEqual(stored_run["node_execution_ids"], ["nodeexec_failed_1", new_node["id"]])
        self.assertEqual(stored_run["current_node_execution_id"], new_node["id"])

    def test_retry_node_execution_enforces_retry_limit(self):
        created_task = asyncio.run(server.create_task({"title": "Retry Limit"}))
        task_id = created_task["task"]["id"]
        run = asyncio.run(server.create_run({"id": "run_retry_limit", "task_id": task_id}))
        node = asyncio.run(server.create_node_execution({
            "id": "nodeexec_retry_limit",
            "run_id": run["run"]["id"],
            "node_key": "builder",
            "status": "failed",
            "retry_count": task_store.MAX_NODE_RETRY_COUNT,
        }))

        response = asyncio.run(server.retry_node_execution(node["nodeExecution"]["id"]))
        self.assertEqual(response.status_code, 400)
        self.assertIn("retry limit reached", self._decode_error(response)["error"].lower())

    def test_transition_run_can_cancel_active_run(self):
        created_task = asyncio.run(server.create_task({"title": "Cancelable Run"}))
        task_id = created_task["task"]["id"]
        run = asyncio.run(server.create_run({"id": "run_cancel_api", "task_id": task_id}))
        transitioned = asyncio.run(server.transition_run(run["run"]["id"], {"status": "running"}))
        self.assertTrue(transitioned["success"])

        cancelled = asyncio.run(server.transition_run(run["run"]["id"], {"status": "cancelled"}))
        self.assertTrue(cancelled["success"])
        self.assertEqual(cancelled["run"]["status"], "cancelled")
        self.assertGreater(cancelled["run"]["ended_at"], 0)

    def test_save_artifact_validates_parent_linkage_and_derives_run_id(self):
        missing_parent = asyncio.run(server.save_artifact({"title": "orphan"}))
        self.assertEqual(missing_parent.status_code, 400)

        created_task = asyncio.run(server.create_task({"title": "Artifact Parent"}))
        task_id = created_task["task"]["id"]
        run = asyncio.run(server.create_run({"id": "run_for_artifact", "task_id": task_id}))
        node = asyncio.run(server.create_node_execution({
            "id": "nodeexec_for_artifact",
            "run_id": run["run"]["id"],
            "node_key": "reviewer",
        }))

        created_artifact = asyncio.run(server.save_artifact({
            "id": "artifact_api_1",
            "nodeExecutionId": node["nodeExecution"]["id"],
            "type": "review_result",
            "title": "Review output",
        }))
        self.assertEqual(created_artifact["artifact"]["run_id"], run["run"]["id"])
        self.assertEqual(created_artifact["artifact"]["node_execution_id"], node["nodeExecution"]["id"])

        node_after = task_store.get_node_execution_store().get_node_execution(node["nodeExecution"]["id"])
        self.assertEqual(node_after["artifact_ids"], ["artifact_api_1"])

        mismatched = asyncio.run(server.save_artifact({
            "run_id": "another-run",
            "node_execution_id": node["nodeExecution"]["id"],
            "title": "bad link",
        }))
        self.assertEqual(mismatched.status_code, 400)
        self.assertIn("does not belong", self._decode_error(mismatched)["error"])

    def test_skills_endpoint_lists_builtin_and_community_skills(self):
        community_dir = Path(self.tmpdir.name) / "community-skills"
        skill_dir = community_dir / "sample-video-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("SAMPLE VIDEO SKILL\n\n- Sample community skill.\n", encoding="utf-8")
        (skill_dir / "evermind_skill.json").write_text(
            '{"title":"Sample Video Skill","summary":"Sample summary","node_types":["builder"],"keywords":["video prompt"],"tags":["video"]}',
            encoding="utf-8",
        )

        with patch.object(agent_skills, "USER_SKILLS_DIR", community_dir):
            agent_skills.list_skill_catalog.cache_clear()
            agent_skills._load_skill.cache_clear()
            result = asyncio.run(server.list_skills())
            self.assertIn("skills", result)
            names = {item["name"] for item in result["skills"]}
            self.assertIn("sample-video-skill", names)
            self.assertIn("remotion-scene-composer", names)
            self.assertGreaterEqual(result["counts"]["community"], 1)
            agent_skills.list_skill_catalog.cache_clear()
            agent_skills._load_skill.cache_clear()


class TestTaskApiCors(unittest.TestCase):
    def test_put_preflight_is_allowed(self):
        client = TestClient(server.app)
        response = client.options(
            "/api/tasks/demo",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("PUT", response.headers.get("access-control-allow-methods", ""))


if __name__ == "__main__":
    unittest.main()
