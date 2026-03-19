"""
Tests for P0-2: TaskStore.project_task_from_run()
Validates unified task projection rules from run lifecycle events.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from task_store import TaskStore, VALID_STATUSES, VALID_TRANSITIONS


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Provide a fresh TaskStore that writes to a temp directory."""
    monkeypatch.setattr("task_store.STORE_DIR", tmp_path)
    monkeypatch.setattr("task_store.TASKS_FILE", tmp_path / "tasks.json")
    s = TaskStore()
    s.load()
    return s


@pytest.fixture
def task_in_executing(store):
    """Create a task in 'executing' status for projection tests."""
    task = store.create_task({"title": "Build auth module", "description": "Test task"})
    store.transition_task(task["id"], "planned")
    store.transition_task(task["id"], "executing")
    return store.get_task(task["id"])


# ─── Review Verdict Projection ───

class TestReviewProjection:

    def test_projects_review_verdict_and_issues(self, store, task_in_executing):
        tid = task_in_executing["id"]
        result = store.project_task_from_run(
            tid,
            review_verdict="needs_fix",
            review_issues=["Missing error handling", "No tests"],
            remaining_risks=["Auth bypass possible"],
            run_id="run_001",
        )
        assert result is not None
        assert result["review_verdict"] == "needs_fix"
        assert result["review_issues"] == ["Missing error handling", "No tests"]
        assert result["latest_risk"] == "Auth bypass possible"
        assert "run_001" in result["run_ids"]

    def test_review_approve_clears_old_verdict(self, store, task_in_executing):
        tid = task_in_executing["id"]
        store.project_task_from_run(tid, review_verdict="reject", review_issues=["Bad code"])
        result = store.project_task_from_run(tid, review_verdict="approve", review_issues=[])
        assert result["review_verdict"] == "approve"
        assert result["review_issues"] == []

    def test_review_does_not_change_task_status(self, store, task_in_executing):
        tid = task_in_executing["id"]
        result = store.project_task_from_run(tid, review_verdict="reject")
        # Review verdict alone doesn't transition task status
        assert result["status"] == "executing"

    def test_waiting_review_projects_task_status_to_review(self, store, task_in_executing):
        tid = task_in_executing["id"]
        result = store.project_task_from_run(
            tid,
            run_status="waiting_review",
            review_verdict="needs_fix",
        )
        assert result["status"] == "review"
        assert result["progress"] == 60


# ─── Selfcheck Projection ───

class TestSelfcheckProjection:

    def test_projects_selfcheck_items(self, store, task_in_executing):
        tid = task_in_executing["id"]
        checklist = [
            {"name": "Unit tests pass", "passed": True, "detail": "42/42"},
            {"name": "Build succeeds", "passed": True, "detail": "0 errors"},
            {"name": "Lint clean", "passed": False, "detail": "3 warnings"},
        ]
        result = store.project_task_from_run(
            tid,
            selfcheck_items=checklist,
            summary="2/3 checks passed",
        )
        assert result is not None
        assert len(result["selfcheck_items"]) == 3
        assert result["selfcheck_items"][0]["name"] == "Unit tests pass"
        assert result["selfcheck_items"][0]["passed"] is True
        assert result["selfcheck_items"][2]["passed"] is False
        assert result["latest_summary"] == "2/3 checks passed"

    def test_empty_selfcheck_clears_items(self, store, task_in_executing):
        tid = task_in_executing["id"]
        store.project_task_from_run(tid, selfcheck_items=[{"name": "test", "passed": True, "detail": "ok"}])
        result = store.project_task_from_run(tid, selfcheck_items=[])
        assert result["selfcheck_items"] == []

    def test_waiting_selfcheck_projects_task_status(self, store, task_in_executing):
        tid = task_in_executing["id"]
        result = store.project_task_from_run(
            tid,
            run_status="waiting_selfcheck",
            selfcheck_items=[{"name": "Preview", "passed": False, "detail": "misaligned"}],
        )
        assert result["status"] == "selfcheck"
        assert result["progress"] == 80


# ─── Run Completion Projection ───

class TestRunCompletionProjection:

    def test_success_transitions_to_review(self, store, task_in_executing):
        tid = task_in_executing["id"]
        result = store.project_task_from_run(
            tid,
            run_status="done",
            summary="All tests pass, deployment ready",
            remaining_risks=["Minor: CSS alignment"],
            run_id="run_002",
        )
        assert result is not None
        assert result["status"] == "review"
        assert result["progress"] == 60
        assert result["latest_summary"] == "All tests pass, deployment ready"
        assert result["latest_risk"] == "Minor: CSS alignment"
        assert "run_002" in result["run_ids"]

    def test_failure_keeps_executing(self, store, task_in_executing):
        tid = task_in_executing["id"]
        result = store.project_task_from_run(
            tid,
            run_status="failed",
            summary="Build failed: 3 errors",
        )
        assert result is not None
        # executing → executing is not actually a transition (same status)
        assert result["status"] == "executing"
        assert result["latest_summary"] == "Build failed: 3 errors"

    def test_invalid_transition_skipped_safely(self, store):
        """If task is already 'done', projecting run_status='done'→'review' should be skipped."""
        task = store.create_task({"title": "Done task"})
        tid = task["id"]
        store.transition_task(tid, "planned")
        store.transition_task(tid, "executing")
        store.transition_task(tid, "review")
        store.transition_task(tid, "selfcheck")
        store.transition_task(tid, "done")
        result = store.project_task_from_run(tid, run_status="done")
        # done → review is invalid (done only allows → backlog)
        assert result["status"] == "done"


# ─── Edge Cases ───

class TestProjectionEdgeCases:

    def test_nonexistent_task_returns_none(self, store):
        result = store.project_task_from_run("fake_task_id", summary="test")
        assert result is None

    def test_idempotent_projection(self, store, task_in_executing):
        tid = task_in_executing["id"]
        r1 = store.project_task_from_run(tid, review_verdict="approve", review_issues=["a"])
        r2 = store.project_task_from_run(tid, review_verdict="approve", review_issues=["a"])
        # Second call is a no-op for verdict (same value), but issues list always updates
        assert r1["review_verdict"] == r2["review_verdict"]

    def test_run_id_not_duplicated(self, store, task_in_executing):
        tid = task_in_executing["id"]
        store.project_task_from_run(tid, run_id="run_x")
        store.project_task_from_run(tid, run_id="run_x")
        result = store.get_task(tid)
        assert result["run_ids"].count("run_x") == 1

    def test_combined_projection(self, store, task_in_executing):
        """A single call can project review + selfcheck + status + summary."""
        tid = task_in_executing["id"]
        result = store.project_task_from_run(
            tid,
            run_status="done",
            review_verdict="approve",
            review_issues=[],
            selfcheck_items=[{"name": "Tests", "passed": True, "detail": "ok"}],
            summary="All good",
            run_id="run_combo",
        )
        assert result["status"] == "review"
        assert result["review_verdict"] == "approve"
        assert len(result["selfcheck_items"]) == 1
        assert result["latest_summary"] == "All good"
        assert "run_combo" in result["run_ids"]

    def test_empty_risks_clear_latest_risk(self, store, task_in_executing):
        tid = task_in_executing["id"]
        store.project_task_from_run(tid, remaining_risks=["Stale risk"])
        result = store.project_task_from_run(tid, remaining_risks=[])
        assert result["latest_risk"] == ""
