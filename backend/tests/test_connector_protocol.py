import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
import task_store
from connector_idempotency import connector_idempotency


class ConnectorProtocolWsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tasks_file = Path(self.tmpdir.name) / "tasks.json"
        self.reports_file = Path(self.tmpdir.name) / "reports.json"
        self.runs_file = Path(self.tmpdir.name) / "runs.json"
        self.node_executions_file = Path(self.tmpdir.name) / "node_executions.json"
        self.artifacts_file = Path(self.tmpdir.name) / "artifacts.json"
        self.patchers = [
            patch.object(task_store, "STORE_DIR", Path(self.tmpdir.name)),
            patch.object(task_store, "TASKS_FILE", self.tasks_file),
            patch.object(task_store, "REPORTS_FILE", self.reports_file),
            patch.object(task_store, "RUNS_FILE", self.runs_file),
            patch.object(task_store, "NODE_EXECUTIONS_FILE", self.node_executions_file),
            patch.object(task_store, "ARTIFACTS_FILE", self.artifacts_file),
        ]
        for patcher in self.patchers:
            patcher.start()
        task_store._task_store = None
        task_store._report_store = None
        task_store._run_store = None
        task_store._node_execution_store = None
        task_store._artifact_store = None
        connector_idempotency.clear()
        server.connected_clients.clear()
        server._active_tasks.clear()
        server._openclaw_dispatch_watchdogs.clear()

    def tearDown(self):
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
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmpdir.cleanup()

    def _create_task_run_node(self, *, run_id: str, node_id: str, node_key: str = "builder", runtime: str = "local"):
        created_task = asyncio.run(server.create_task({"title": f"Task for {run_id}"}))
        task_id = created_task["task"]["id"]
        run = asyncio.run(server.create_run({"id": run_id, "task_id": task_id, "runtime": runtime}))
        node = asyncio.run(server.create_node_execution({
            "id": node_id,
            "run_id": run["run"]["id"],
            "node_key": node_key,
            "node_label": node_key.title(),
        }))
        return task_id, run["run"]["id"], node["nodeExecution"]["id"]

    def test_dispatch_node_updates_state_and_broadcasts(self):
        _task_id, run_id, node_id = self._create_task_run_node(run_id="run_dispatch", node_id="node_dispatch")

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                ui_ws.send_json({
                    "type": "evermind_dispatch_node",
                    "requestId": "req_dispatch",
                    "idempotencyKey": "dispatch:run_dispatch:node_dispatch",
                    "timestamp": int(time.time() * 1000),
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "nodeKey": "builder",
                    },
                })

                ack = ui_ws.receive_json()
                forwarded = runtime_ws.receive_json()
                node = task_store.get_node_execution_store().get_node_execution(node_id)

                self.assertEqual(ack["type"], "evermind_dispatch_node_ack")
                self.assertTrue(ack["payload"]["dispatched"])
                self.assertEqual(forwarded["type"], "evermind_dispatch_node")
                self.assertEqual(forwarded["payload"]["nodeExecutionId"], node_id)
                self.assertEqual(node["status"], "running")

    def test_attach_artifact_persists_and_links_node_execution(self):
        _task_id, run_id, node_id = self._create_task_run_node(run_id="run_artifact_ws", node_id="node_artifact_ws")

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                payload = {
                    "runId": run_id,
                    "nodeExecutionId": node_id,
                    "artifact": {
                        "id": "artifact_ws_1",
                        "type": "report",
                        "title": "Connector Artifact",
                        "content": "hello connector",
                    },
                }
                runtime_ws.send_json({
                    "type": "openclaw_attach_artifact",
                    "idempotencyKey": "artifact:artifact_ws_1",
                    "payload": payload,
                })
                forwarded = ui_ws.receive_json()
                runtime_ws.send_json({
                    "type": "openclaw_attach_artifact",
                    "idempotencyKey": "artifact:artifact_ws_1",
                    "payload": payload,
                })

                artifact = task_store.get_artifact_store().get_artifact("artifact_ws_1")
                node = task_store.get_node_execution_store().get_node_execution(node_id)
                artifacts = task_store.get_artifact_store().list_artifacts(node_execution_id=node_id)

                self.assertEqual(forwarded["type"], "openclaw_attach_artifact")
                self.assertIsNotNone(artifact)
                self.assertEqual(artifact["run_id"], run_id)
                self.assertEqual(node["artifact_ids"], ["artifact_ws_1"])
                self.assertEqual(len(artifacts), 1)

    def test_node_update_persists_metrics_and_ignores_stale_updates(self):
        _task_id, run_id, node_id = self._create_task_run_node(run_id="run_node_update", node_id="node_node_update")

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": "update:node_node_update:1",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "status": "running",
                        "tokensUsed": 321,
                        "costDelta": 0.75,
                        "partialOutputSummary": "partial output",
                        "timestamp": int(time.time() * 1000),
                    },
                })
                first_forwarded = ui_ws.receive_json()
                self.assertEqual(first_forwarded["type"], "openclaw_node_update")
                self.assertGreater(first_forwarded["payload"]["startedAt"], 0)
                self.assertEqual(first_forwarded["payload"]["nodeExecutionId"], node_id)
                self.assertEqual(first_forwarded["payload"]["nodeKey"], "builder")
                self.assertEqual(first_forwarded["payload"]["tokensUsed"], 321)
                self.assertAlmostEqual(first_forwarded["payload"]["cost"], 0.75, places=6)

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": "update:node_node_update:stale",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "status": "failed",
                        "timestamp": 1,
                    },
                })
                self.assertEqual(ui_ws.receive_json()["type"], "openclaw_node_update")

                node = task_store.get_node_execution_store().get_node_execution(node_id)
                run = task_store.get_run_store().get_run(run_id)

                self.assertEqual(node["status"], "running")
                self.assertEqual(node["tokens_used"], 321)
                self.assertAlmostEqual(node["cost"], 0.75, places=6)
                self.assertEqual(node["output_summary"], "partial output")
                self.assertEqual(run["current_node_execution_id"], node_id)

    def test_node_update_accepts_skipped_status_and_auto_completes_done_run(self):
        task_id, run_id, node_id = self._create_task_run_node(
            run_id="run_node_skipped",
            node_id="node_skipped",
            runtime="openclaw",
        )
        asyncio.run(server.transition_run(run_id, {"status": "running"}))

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": "update:node_skipped:1",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "status": "skipped",
                        "timestamp": int(time.time() * 1000),
                    },
                })
                first = ui_ws.receive_json()
                second = ui_ws.receive_json()

                node = task_store.get_node_execution_store().get_node_execution(node_id)
                run = task_store.get_run_store().get_run(run_id)
                task = task_store.get_task_store().get_task(task_id)

                self.assertEqual(first["type"], "openclaw_node_update")
                self.assertEqual(second["type"], "openclaw_run_complete")
                self.assertEqual(node["status"], "skipped")
                self.assertEqual(run["status"], "done")
                self.assertEqual(run["active_node_execution_ids"], [])
                self.assertEqual(task["status"], "done")

    def test_node_update_accepts_cancelled_status(self):
        _task_id, run_id, node_id = self._create_task_run_node(
            run_id="run_node_cancelled",
            node_id="node_cancelled",
        )
        asyncio.run(server.transition_run(run_id, {"status": "running"}))

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": "update:node_cancelled:running",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "status": "running",
                        "timestamp": int(time.time() * 1000),
                    },
                })
                self.assertEqual(ui_ws.receive_json()["type"], "openclaw_node_update")

                runtime_ws.send_json({
                    "type": "openclaw_node_update",
                    "idempotencyKey": "update:node_cancelled:cancelled",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "status": "cancelled",
                        "timestamp": int(time.time() * 1000),
                    },
                })
                self.assertEqual(ui_ws.receive_json()["type"], "openclaw_node_update")

                node = task_store.get_node_execution_store().get_node_execution(node_id)
                run = task_store.get_run_store().get_run(run_id)
                self.assertEqual(node["status"], "cancelled")
                self.assertEqual(run["active_node_execution_ids"], [])

    def test_review_submission_persists_artifact_and_marks_run_waiting_review(self):
        task_id, run_id, node_id = self._create_task_run_node(run_id="run_review_ws", node_id="node_review_ws", node_key="reviewer")
        asyncio.run(server.transition_run(run_id, {"status": "running"}))
        asyncio.run(server.transition_node_execution(node_id, {"status": "running"}))

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                runtime_ws.send_json({
                    "type": "openclaw_submit_review",
                    "idempotencyKey": "review:node_review_ws",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "decision": "needs_fix",
                        "issues": ["Missing tests", "Risky refactor"],
                        "remainingRisks": ["Merge conflict risk"],
                        "nextAction": "Revise implementation",
                    },
                })
                forwarded = ui_ws.receive_json()

                node = task_store.get_node_execution_store().get_node_execution(node_id)
                run = task_store.get_run_store().get_run(run_id)
                task = task_store.get_task_store().get_task(task_id)
                artifacts = task_store.get_artifact_store().list_artifacts(node_execution_id=node_id)

                self.assertEqual(forwarded["type"], "openclaw_submit_review")
                self.assertEqual(forwarded["payload"]["taskId"], task_id)
                self.assertEqual(node["status"], "passed")
                self.assertIn("Review: needs_fix", node["output_summary"])
                self.assertEqual(run["status"], "waiting_review")
                self.assertEqual(task["status"], "review")
                self.assertEqual(task["review_verdict"], "needs_fix")
                self.assertEqual(task["review_issues"], ["Missing tests", "Risky refactor"])
                self.assertEqual(len(artifacts), 1)
                self.assertEqual(artifacts[0]["artifact_type"], "review_result")

    def test_validation_submission_persists_artifact_and_marks_run_waiting_selfcheck(self):
        task_id, run_id, node_id = self._create_task_run_node(run_id="run_validation_ws", node_id="node_validation_ws", node_key="tester")
        asyncio.run(server.transition_run(run_id, {"status": "running"}))
        asyncio.run(server.transition_node_execution(node_id, {"status": "running"}))

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                runtime_ws.send_json({
                    "type": "openclaw_submit_validation",
                    "idempotencyKey": "validation:node_validation_ws",
                    "payload": {
                        "runId": run_id,
                        "nodeExecutionId": node_id,
                        "summaryStatus": "blocked",
                        "summary": "Visual regression found",
                        "checklist": [{"name": "Preview", "passed": False, "detail": "Misaligned layout"}],
                    },
                })
                forwarded = ui_ws.receive_json()

                node = task_store.get_node_execution_store().get_node_execution(node_id)
                run = task_store.get_run_store().get_run(run_id)
                task = task_store.get_task_store().get_task(task_id)
                artifacts = task_store.get_artifact_store().list_artifacts(node_execution_id=node_id)

                self.assertEqual(forwarded["type"], "openclaw_submit_validation")
                self.assertEqual(forwarded["payload"]["taskId"], task_id)
                self.assertEqual(node["status"], "passed")
                self.assertEqual(node["output_summary"], "Visual regression found")
                self.assertEqual(run["status"], "waiting_selfcheck")
                self.assertEqual(task["status"], "selfcheck")
                self.assertEqual(task["latest_summary"], "Visual regression found")
                self.assertEqual(len(task["selfcheck_items"]), 1)
                self.assertEqual(len(artifacts), 1)
                self.assertEqual(artifacts[0]["artifact_type"], "report")

    def test_run_complete_projects_task_done_and_enriches_broadcast_payload(self):
        task_id, run_id, _node_id = self._create_task_run_node(run_id="run_complete_ws", node_id="node_complete_ws")
        asyncio.run(server.transition_run(run_id, {"status": "running"}))

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                runtime_ws.send_json({
                    "type": "openclaw_run_complete",
                    "idempotencyKey": "complete:run_complete_ws",
                    "payload": {
                        "runId": run_id,
                        "finalResult": "success",
                        "summary": "All checks passed",
                        "risks": [],
                        "totalTokens": 123,
                        "totalCost": 0.45,
                    },
                })
                forwarded = ui_ws.receive_json()

                run = task_store.get_run_store().get_run(run_id)
                task = task_store.get_task_store().get_task(task_id)

                self.assertEqual(forwarded["type"], "openclaw_run_complete")
                self.assertEqual(forwarded["payload"]["taskId"], task_id)
                self.assertEqual(run["status"], "done")
                self.assertEqual(run["summary"], "All checks passed")
                self.assertEqual(task["status"], "done")
                self.assertEqual(task["latest_summary"], "All checks passed")

    def test_run_complete_backfills_metrics_and_preview_url_from_canonical_state(self):
        task_id, run_id, node_id = self._create_task_run_node(
            run_id="run_complete_backfill",
            node_id="node_complete_backfill",
            runtime="openclaw",
        )
        preview_file = Path(self.tmpdir.name) / "index.html"
        preview_file.write_text("<!DOCTYPE html><html><body>ok</body></html>", encoding="utf-8")
        asyncio.run(server.transition_run(run_id, {"status": "running"}))
        task_store.get_node_execution_store().transition_node(node_id, "running")
        task_store.get_node_execution_store().update_node_execution(node_id, {
            "tokens_used": 456,
            "cost": 1.25,
        })
        task_store.get_node_execution_store().transition_node(node_id, "passed")

        with patch.object(server, "latest_preview_artifact", return_value=("root", preview_file)):
            with patch.object(server, "build_preview_url_for_file", return_value="http://127.0.0.1:8765/preview/index.html"):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                        self.assertEqual(ui_ws.receive_json()["type"], "connected")
                        self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                        runtime_ws.send_json({
                            "type": "openclaw_run_complete",
                            "idempotencyKey": "complete:run_complete_backfill",
                            "payload": {
                                "runId": run_id,
                                "taskId": task_id,
                                "finalResult": "success",
                                "summary": "Connector completed",
                                "totalTokens": 0,
                                "totalCost": 0,
                            },
                        })

                        forwarded = ui_ws.receive_json()
                        preview_ready = ui_ws.receive_json()
                        run = task_store.get_run_store().get_run(run_id)

                        self.assertEqual(forwarded["type"], "openclaw_run_complete")
                        self.assertEqual(forwarded["payload"]["totalTokens"], 456)
                        self.assertEqual(forwarded["payload"]["totalCost"], 1.25)
                        self.assertEqual(forwarded["payload"]["previewUrl"], "http://127.0.0.1:8765/preview/index.html")
                        self.assertEqual(preview_ready["type"], "preview_ready")
                        self.assertEqual(preview_ready["preview_url"], "http://127.0.0.1:8765/preview/index.html")
                        self.assertIsNotNone(run)
                        self.assertEqual(run["total_tokens"], 456)
                        self.assertEqual(run["total_cost"], 1.25)

    def test_cancel_then_resume_run_broadcasts_and_restores_running_state(self):
        _task_id, run_id, _node_id = self._create_task_run_node(run_id="run_cancel_resume_ws", node_id="node_cancel_resume_ws")
        asyncio.run(server.transition_run(run_id, {"status": "running"}))

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ui_ws, client.websocket_connect("/ws") as runtime_ws:
                self.assertEqual(ui_ws.receive_json()["type"], "connected")
                self.assertEqual(runtime_ws.receive_json()["type"], "connected")

                ui_ws.send_json({
                    "type": "evermind_cancel_run",
                    "requestId": "req_cancel",
                    "idempotencyKey": f"cancel:{run_id}",
                    "payload": {"runId": run_id},
                })
                cancel_ack = ui_ws.receive_json()
                cancel_forward = runtime_ws.receive_json()

                ui_ws.send_json({
                    "type": "evermind_resume_run",
                    "requestId": "req_resume",
                    "idempotencyKey": f"resume:{run_id}",
                    "payload": {"runId": run_id},
                })
                resume_ack = ui_ws.receive_json()
                resume_forward = runtime_ws.receive_json()
                run = task_store.get_run_store().get_run(run_id)

                self.assertTrue(cancel_ack["payload"]["cancelled"])
                self.assertEqual(cancel_forward["type"], "evermind_cancel_run")
                self.assertTrue(resume_ack["payload"]["resumed"])
                self.assertEqual(resume_forward["type"], "evermind_resume_run")
                self.assertEqual(run["status"], "running")


if __name__ == "__main__":
    unittest.main()
