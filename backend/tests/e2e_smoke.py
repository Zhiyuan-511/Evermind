#!/usr/bin/env python3
"""
P0-4: End‑to‑End Smoke Test for Evermind
=========================================
Exercises the full happy path via REST + WebSocket:

  1. Create a task
  2. Transition task → planned → executing
  3. Create a run for the task
  4. Create node executions for the run
  5. Simulate OpenClaw WS events:
       - openclaw_node_update (running → passed)
       - openclaw_submit_review (approve)
       - openclaw_submit_validation (passed)
       - openclaw_run_complete (success)
  6. Verify projections via REST:
       - Task status, version, review_verdict, selfcheck_items, summary
       - Run status, version
       - NodeExecution status, version

Usage:
    python backend/tests/e2e_smoke.py [--base-url http://127.0.0.1:8765]

Requires: pip install httpx websockets
"""

import asyncio
import argparse
import json
import sys
import time
import uuid

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets")
    sys.exit(1)


# ── Helpers ──

def uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


class SmokeAssertionError(Exception):
    pass


def check(condition: bool, msg: str):
    if not condition:
        raise SmokeAssertionError(f"FAIL: {msg}")


def ok(msg: str):
    print(f"  ✓ {msg}")


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def wait_until(label: str, fetch, predicate, timeout: float = 5.0, interval: float = 0.1):
    """Poll until the expected state is visible via REST."""
    deadline = time.time() + timeout
    last_value = None
    while time.time() < deadline:
        last_value = fetch()
        if predicate(last_value):
            return last_value
        time.sleep(interval)
    raise SmokeAssertionError(f"FAIL: Timed out waiting for {label}. Last value: {last_value}")


# ── REST helpers ──

class API:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(timeout=10)

    def create_task(self, title: str, description: str = "") -> dict:
        r = self.client.post(f"{self.base}/api/tasks", json={
            "title": title,
            "description": description or f"E2E test task: {title}",
            "priority": "high",
            "mode": "standard",
        })
        r.raise_for_status()
        return r.json()["task"]

    def get_task(self, task_id: str) -> dict:
        r = self.client.get(f"{self.base}/api/tasks/{task_id}")
        r.raise_for_status()
        return r.json()["task"]

    def transition_task(self, task_id: str, status: str) -> dict:
        r = self.client.post(f"{self.base}/api/tasks/{task_id}/transition", json={"status": status})
        r.raise_for_status()
        return r.json()

    def create_run(self, task_id: str, run_id: str = "", trigger: str = "api") -> dict:
        payload = {"task_id": task_id, "trigger_source": trigger}
        if run_id:
            payload["id"] = run_id
        r = self.client.post(f"{self.base}/api/runs", json=payload)
        r.raise_for_status()
        return r.json()["run"]

    def get_run(self, run_id: str) -> dict:
        r = self.client.get(f"{self.base}/api/runs/{run_id}")
        r.raise_for_status()
        return r.json()["run"]

    def transition_run(self, run_id: str, status: str) -> dict:
        r = self.client.post(f"{self.base}/api/runs/{run_id}/transition", json={"status": status})
        r.raise_for_status()
        return r.json()

    def create_node_execution(self, run_id: str, node_key: str, ne_id: str = "") -> dict:
        payload = {
            "run_id": run_id,
            "node_key": node_key,
            "node_label": node_key.title(),
        }
        if ne_id:
            payload["id"] = ne_id
        r = self.client.post(f"{self.base}/api/node-executions", json=payload)
        r.raise_for_status()
        return r.json()["nodeExecution"]

    def get_node_execution(self, ne_id: str) -> dict:
        r = self.client.get(f"{self.base}/api/node-executions/{ne_id}")
        r.raise_for_status()
        return r.json()["nodeExecution"]

    def list_artifacts(self, run_id: str = "", node_execution_id: str = "") -> list[dict]:
        params = {}
        if run_id:
            params["runId"] = run_id
        if node_execution_id:
            params["nodeExecutionId"] = node_execution_id
        r = self.client.get(f"{self.base}/api/artifacts", params=params)
        r.raise_for_status()
        return r.json()["artifacts"]

    def close(self):
        self.client.close()


# ── WebSocket OpenClaw simulator ──

async def send_openclaw_event(ws_url: str, msg_type: str, payload: dict, idem_key: str = ""):
    """Send a single OpenClaw event via WebSocket, then disconnect."""
    uri = ws_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    async with websockets.connect(uri) as ws:
        message = {
            "type": msg_type,
            "payload": payload,
        }
        if idem_key:
            message["idempotencyKey"] = idem_key
        await ws.send(json.dumps(message))


# ── Test Phases ──

def phase_1_create_entities(api: API) -> tuple:
    """Create task, run, and node executions."""
    section("Phase 1: Create Entities")

    # Create task
    task = api.create_task("Auth Service Implementation", "Build JWT auth with refresh tokens")
    task_id = task["id"]
    check(task["status"] == "backlog", f"New task should be backlog, got: {task['status']}")
    check(task.get("version") is not None, "Task should have version field")
    ok(f"Task created: {task_id} (status={task['status']}, version={task.get('version')})")

    # Create run (auto-transitions task to executing)
    run_id = uid("run_smoke_")
    run = api.create_run(task_id, run_id=run_id)
    check(run["id"] == run_id, f"Run ID mismatch: expected {run_id}, got {run['id']}")
    check(run["status"] == "queued", f"New run should be queued, got: {run['status']}")
    check(run.get("version", 0) >= 1, f"Run version should be >= 1, got: {run.get('version')}")
    ok(f"Run created: {run_id} (status={run['status']}, version={run.get('version')})")

    # Verify task auto-transitioned to executing
    task = api.get_task(task_id)
    check(task["status"] == "executing", f"Task should auto-transition to executing, got: {task['status']}")
    ok(f"Task auto-transitioned to executing (version={task.get('version')})")

    # Transition run to running
    api.transition_run(run_id, "running")
    run = api.get_run(run_id)
    check(run["status"] == "running", f"Run should be running, got: {run['status']}")
    ok(f"Run transitioned to running (version={run.get('version')})")

    # Create node executions
    ne_planner_id = uid("ne_planner_")
    ne_builder_id = uid("ne_builder_")
    ne_tester_id = uid("ne_tester_")

    ne_planner = api.create_node_execution(run_id, "planner", ne_planner_id)
    ne_builder = api.create_node_execution(run_id, "builder", ne_builder_id)
    ne_tester = api.create_node_execution(run_id, "tester", ne_tester_id)

    for ne, label in [(ne_planner, "planner"), (ne_builder, "builder"), (ne_tester, "tester")]:
        check(ne["status"] == "queued", f"{label} should be queued, got: {ne['status']}")
        check(ne.get("version", 0) >= 1, f"{label} version should be >= 1")
        ok(f"NodeExecution '{label}' created: {ne['id']} (version={ne.get('version')})")

    return task_id, run_id, ne_planner_id, ne_builder_id, ne_tester_id


async def phase_2_simulate_execution(api: API, base_url: str, task_id: str, run_id: str,
                                      ne_planner_id: str, ne_builder_id: str, ne_tester_id: str):
    """Simulate OpenClaw executing nodes via WS events."""
    section("Phase 2: Simulate OpenClaw Node Execution")

    now = time.time()

    # 2a. Planner → running → passed
    await send_openclaw_event(base_url, "openclaw_node_update", {
        "nodeExecutionId": ne_planner_id,
        "runId": run_id,
        "status": "running",
        "timestamp": now,
        "partialOutputSummary": "Analyzing architecture...",
    }, idem_key=uid("idem_"))

    ne = wait_until(
        "planner status=running",
        lambda: api.get_node_execution(ne_planner_id),
        lambda item: item["status"] == "running",
    )
    check(ne["status"] == "running", f"Planner should be running, got: {ne['status']}")
    ok(f"Planner → running (version={ne.get('version')})")

    await send_openclaw_event(base_url, "openclaw_node_update", {
        "nodeExecutionId": ne_planner_id,
        "runId": run_id,
        "status": "passed",
        "timestamp": now + 1,
        "partialOutputSummary": "Architecture plan complete: 3 microservices",
    }, idem_key=uid("idem_"))

    ne = wait_until(
        "planner status=passed",
        lambda: api.get_node_execution(ne_planner_id),
        lambda item: item["status"] == "passed",
    )
    check(ne["status"] == "passed", f"Planner should be passed, got: {ne['status']}")
    ok(f"Planner → passed (version={ne.get('version')})")

    # 2b. Builder → running → passed
    await send_openclaw_event(base_url, "openclaw_node_update", {
        "nodeExecutionId": ne_builder_id,
        "runId": run_id,
        "status": "running",
        "timestamp": now + 2,
    }, idem_key=uid("idem_"))

    await send_openclaw_event(base_url, "openclaw_node_update", {
        "nodeExecutionId": ne_builder_id,
        "runId": run_id,
        "status": "passed",
        "timestamp": now + 3,
        "partialOutputSummary": "JWT auth module built: 4 files, 280 LOC",
        "tokensUsed": 12500,
        "cost": 0.045,
    }, idem_key=uid("idem_"))

    ne = wait_until(
        "builder status=passed",
        lambda: api.get_node_execution(ne_builder_id),
        lambda item: item["status"] == "passed" and item.get("tokens_used", 0) == 12500,
    )
    check(ne["status"] == "passed", f"Builder should be passed, got: {ne['status']}")
    check(ne.get("tokens_used", 0) == 12500, f"Builder tokens should be 12500")
    ok(f"Builder → passed (tokens={ne.get('tokens_used')}, version={ne.get('version')})")

    # 2c. Tester → running → passed
    await send_openclaw_event(base_url, "openclaw_node_update", {
        "nodeExecutionId": ne_tester_id,
        "runId": run_id,
        "status": "running",
        "timestamp": now + 4,
    }, idem_key=uid("idem_"))

    await send_openclaw_event(base_url, "openclaw_node_update", {
        "nodeExecutionId": ne_tester_id,
        "runId": run_id,
        "status": "passed",
        "timestamp": now + 5,
        "partialOutputSummary": "All 12 tests pass, 94% coverage",
    }, idem_key=uid("idem_"))

    ne = wait_until(
        "tester status=passed",
        lambda: api.get_node_execution(ne_tester_id),
        lambda item: item["status"] == "passed",
    )
    check(ne["status"] == "passed", f"Tester should be passed, got: {ne['status']}")
    ok(f"Tester → passed (version={ne.get('version')})")

    # Verify run is still running at this point
    run = wait_until(
        "run remains running after node execution",
        lambda: api.get_run(run_id),
        lambda item: item["status"] == "running",
    )
    check(run["status"] == "running", f"Run should still be running, got: {run['status']}")
    ok(f"Run still running after all nodes pass (version={run.get('version')})")


async def phase_3_review_and_selfcheck(api: API, base_url: str, task_id: str, run_id: str):
    """Simulate review and validation events."""
    section("Phase 3: Review & Self-Check")

    now = time.time()

    # 3a. Submit review — needs_fix (negative) → blocks run
    await send_openclaw_event(base_url, "openclaw_submit_review", {
        "taskId": task_id,
        "runId": run_id,
        "decision": "needs_fix",
        "issues": ["Missing rate limiting on /auth/login", "No input validation on email field"],
        "remainingRisks": ["Brute force attack vector"],
        "timestamp": now,
    }, idem_key=uid("idem_"))

    task = wait_until(
        "task projected to review with needs_fix verdict",
        lambda: api.get_task(task_id),
        lambda item: item.get("reviewVerdict") == "needs_fix" and item["status"] == "review",
    )
    check(task.get("reviewVerdict") == "needs_fix", f"Review verdict should be needs_fix, got: {task.get('reviewVerdict')}")
    check(len(task.get("reviewIssues", [])) == 2, f"Should have 2 review issues, got: {len(task.get('reviewIssues', []))}")
    check(task.get("latestRisk") == "Brute force attack vector", f"Latest risk mismatch")
    check(task["status"] == "review", f"Task should be in review after negative review, got: {task['status']}")
    ok(f"Review needs_fix projected → task status={task['status']}, verdict={task.get('reviewVerdict')}, issues={len(task.get('reviewIssues', []))}")

    # Verify run transitioned to waiting_review
    run = wait_until(
        "run status=waiting_review",
        lambda: api.get_run(run_id),
        lambda item: item["status"] == "waiting_review",
    )
    check(run["status"] == "waiting_review", f"Run should be waiting_review, got: {run['status']}")
    ok(f"Run → waiting_review (version={run.get('version')})")

    # 3b. Resume run (simulate fix applied, agent re-starts)
    api.transition_run(run_id, "running")
    run = api.get_run(run_id)
    check(run["status"] == "running", f"Run should be running after resume, got: {run['status']}")
    ok(f"Run resumed → running (version={run.get('version')})")

    # 3c. Submit review — approve (positive, after fix)
    await send_openclaw_event(base_url, "openclaw_submit_review", {
        "taskId": task_id,
        "runId": run_id,
        "decision": "approve",
        "issues": [],
        "remainingRisks": [],
        "timestamp": now + 1,
    }, idem_key=uid("idem_"))

    task = wait_until(
        "task projected to approve verdict",
        lambda: api.get_task(task_id),
        lambda item: item.get("reviewVerdict") == "approve" and len(item.get("reviewIssues", [])) == 0,
    )
    check(task.get("reviewVerdict") == "approve", f"Review verdict should be approve, got: {task.get('reviewVerdict')}")
    check(len(task.get("reviewIssues", [])) == 0, f"Should have 0 review issues after approve")
    check(task.get("latestRisk") == "", f"Latest risk should be cleared, got: {task.get('latestRisk')!r}")
    ok(f"Review approve projected → verdict={task.get('reviewVerdict')}, risk cleared")

    # 3d. Submit validation — passed
    await send_openclaw_event(base_url, "openclaw_submit_validation", {
        "taskId": task_id,
        "runId": run_id,
        "summaryStatus": "passed",
        "summary": "All quality gates passed",
        "checklist": [
            {"name": "Unit tests", "status": "passed", "detail": "12/12 pass"},
            {"name": "Integration tests", "status": "passed", "detail": "4/4 pass"},
            {"name": "Lint check", "status": "passed", "detail": "0 warnings"},
        ],
        "timestamp": now + 2,
    }, idem_key=uid("idem_"))

    task = wait_until(
        "task projected selfcheck items",
        lambda: api.get_task(task_id),
        lambda item: len(item.get("selfcheckItems", [])) == 3 and item.get("latestSummary") == "All quality gates passed",
    )
    check(len(task.get("selfcheckItems", [])) == 3, f"Should have 3 selfcheck items, got: {len(task.get('selfcheckItems', []))}")
    check(task.get("latestSummary") == "All quality gates passed", f"Summary mismatch")
    ok(f"Validation projected → selfcheckItems={len(task.get('selfcheckItems', []))}, summary='{task.get('latestSummary')}'")


async def phase_4_run_complete(api: API, base_url: str, task_id: str, run_id: str):
    """Simulate run completion and verify final state."""
    section("Phase 4: Run Complete")

    now = time.time()

    await send_openclaw_event(base_url, "openclaw_run_complete", {
        "taskId": task_id,
        "runId": run_id,
        "finalResult": "success",
        "summary": "Auth service fully implemented and tested",
        "risks": ["Minor: Session timeout could be tuned"],
        "totalTokens": 45000,
        "totalCost": 0.15,
        "timestamp": now,
    }, idem_key=uid("idem_"))

    # Verify run is done
    run = wait_until(
        "run status=done",
        lambda: api.get_run(run_id),
        lambda item: item["status"] == "done" and item.get("summary") == "Auth service fully implemented and tested",
    )
    check(run["status"] == "done", f"Run should be done, got: {run['status']}")
    check(run.get("summary") == "Auth service fully implemented and tested", f"Run summary mismatch")
    check(run.get("total_tokens") == 45000, f"Run tokens should be 45000, got: {run.get('total_tokens')}")
    v_run = run.get("version", 0)
    check(v_run >= 3, f"Run version should be >= 3 (create + running + done), got: {v_run}")
    ok(f"Run → done (tokens={run.get('total_tokens')}, cost={run.get('total_cost')}, version={v_run})")

    # Verify task projected to review
    task = wait_until(
        "task projected final review summary",
        lambda: api.get_task(task_id),
        lambda item: item["status"] == "review" and item.get("latestSummary") == "Auth service fully implemented and tested",
    )
    check(task["status"] == "review", f"Task should be in review after successful run, got: {task['status']}")
    check(task.get("latestSummary") == "Auth service fully implemented and tested", f"Task summary mismatch")
    check(task.get("latestRisk") == "Minor: Session timeout could be tuned", f"Task risk mismatch")
    check(run_id in task.get("runIds", []), f"Run ID should be in task's runIds")
    v_task = task.get("version", 0)
    check(v_task >= 3, f"Task version should be >= 3 after multiple updates, got: {v_task}")
    ok(f"Task projected → status={task['status']}, summary present, risk present, version={v_task}")


def phase_5_version_monotonicity(api: API, task_id: str, run_id: str,
                                  ne_planner_id: str, ne_builder_id: str, ne_tester_id: str):
    """Verify version fields are present and monotonically incremented."""
    section("Phase 5: Version Monotonicity")

    task = api.get_task(task_id)
    run = api.get_run(run_id)

    t_ver = task.get("version", -1)
    r_ver = run.get("version", -1)
    check(t_ver >= 0, f"Task should have version >= 0, got: {t_ver}")
    check(r_ver >= 1, f"Run should have version >= 1, got: {r_ver}")
    ok(f"Task version={t_ver}, Run version={r_ver}")

    for ne_id, label in [(ne_planner_id, "planner"), (ne_builder_id, "builder"), (ne_tester_id, "tester")]:
        ne = api.get_node_execution(ne_id)
        v = ne.get("version", -1)
        check(v >= 1, f"{label} version should be >= 1, got: {v}")
        ok(f"NodeExecution '{label}' version={v}")


async def phase_6_idempotency(api: API, base_url: str, run_id: str, node_execution_id: str):
    """Verify that duplicate idempotency keys are rejected."""
    section("Phase 6: Idempotency Guard")

    fixed_idem_key = uid("idem_dup_")
    artifact_id = uid("artifact_smoke_")
    baseline_node = api.get_node_execution(node_execution_id)
    artifact_count_before = len(api.list_artifacts(node_execution_id=node_execution_id))

    # First send should persist the artifact and bump the node version.
    await send_openclaw_event(base_url, "openclaw_attach_artifact", {
        "runId": run_id,
        "nodeExecutionId": node_execution_id,
        "artifact": {
            "id": artifact_id,
            "type": "report",
            "title": "Smoke Artifact",
            "content": "artifact created by e2e smoke test",
        },
    }, idem_key=fixed_idem_key)

    node_after_first = wait_until(
        "artifact attached to node execution",
        lambda: api.get_node_execution(node_execution_id),
        lambda item: artifact_id in item.get("artifact_ids", []),
    )
    version_after_first = node_after_first.get("version", 0)
    check(
        version_after_first > baseline_node.get("version", 0),
        f"Node version should bump after first artifact attach (before={baseline_node.get('version')}, after={version_after_first})",
    )
    ok(f"Artifact attach bumped node version ({baseline_node.get('version')} → {version_after_first})")

    # Send again with same key — should be idempotent (no-op).
    await send_openclaw_event(base_url, "openclaw_attach_artifact", {
        "runId": run_id,
        "nodeExecutionId": node_execution_id,
        "artifact": {
            "id": artifact_id,
            "type": "report",
            "title": "Smoke Artifact",
            "content": "duplicate payload SHOULD NOT BE RE-ATTACHED",
        },
    }, idem_key=fixed_idem_key)

    node_after_second, artifact_count_after = wait_until(
        "duplicate artifact attach stays idempotent",
        lambda: (
            api.get_node_execution(node_execution_id),
            len(api.list_artifacts(node_execution_id=node_execution_id)),
        ),
        lambda result: result[0].get("version", 0) == version_after_first and result[1] == artifact_count_before + 1,
        timeout=1.5,
        interval=0.05,
    )
    version_after_second = node_after_second.get("version", 0)

    check(
        version_after_second == version_after_first,
        f"Node version should not change on duplicate idempotency key (before={version_after_first}, after={version_after_second})",
    )
    check(
        artifact_count_after == artifact_count_before + 1,
        f"Artifact count should increase once only (before={artifact_count_before}, after={artifact_count_after})",
    )
    ok(f"Idempotency guard works: node version unchanged ({version_after_first} → {version_after_second})")


# ── Main ──

async def main(base_url: str):
    print(f"\n{'═' * 60}")
    print(f"  Evermind E2E Smoke Test")
    print(f"  Server: {base_url}")
    print(f"{'═' * 60}")

    try:
        api = API(base_url)

        # Quick health check
        r = api.client.get(f"{base_url}/api/tasks")
        r.raise_for_status()
        ok(f"Server reachable at {base_url}")

        task_id, run_id, ne_planner_id, ne_builder_id, ne_tester_id = phase_1_create_entities(api)
        await phase_2_simulate_execution(api, base_url, task_id, run_id,
                                          ne_planner_id, ne_builder_id, ne_tester_id)
        await phase_3_review_and_selfcheck(api, base_url, task_id, run_id)
        await phase_4_run_complete(api, base_url, task_id, run_id)
        phase_5_version_monotonicity(api, task_id, run_id, ne_planner_id, ne_builder_id, ne_tester_id)
        await phase_6_idempotency(api, base_url, run_id, ne_builder_id)

        section("RESULT")
        print(f"  ✅ ALL PHASES PASSED")
        print(f"\n  Summary:")
        print(f"    Task ID:  {task_id}")
        print(f"    Run ID:   {run_id}")
        print(f"    Nodes:    {ne_planner_id}, {ne_builder_id}, {ne_tester_id}")
        print()

    except httpx.HTTPError as e:
        print(f"\n  ERROR: Cannot reach server at {base_url}")
        print(f"  Details: {e}")
        print(f"\n  Make sure the backend is running:")
        print(f"    cd backend && python server.py")
        sys.exit(1)
    except SmokeAssertionError as e:
        section("RESULT")
        print(f"  ❌ {e}")
        sys.exit(1)
    except Exception as e:
        section("RESULT")
        print(f"  ❌ Unexpected error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if "api" in locals():
            api.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evermind E2E Smoke Test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="Backend base URL")
    args = parser.parse_args()
    asyncio.run(main(args.base_url))
