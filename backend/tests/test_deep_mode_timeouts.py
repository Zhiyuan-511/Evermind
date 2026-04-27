"""V6.1: deep-mode outer timeout uplift tests."""

import pytest
from unittest.mock import MagicMock

from ai_bridge import AIBridge
from orchestrator import Orchestrator


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in (
        "EVERMIND_THINKING_DEPTH",
        "EVERMIND_BUILDER_TIMEOUT_SEC",
        "EVERMIND_PLANNER_TIMEOUT_SEC",
        "EVERMIND_SUBTASK_TIMEOUT_SEC",
    ):
        monkeypatch.delenv(v, raising=False)
    yield


def _orch(depth: str) -> Orchestrator:
    bridge = AIBridge(config={"thinking_depth": depth})
    return Orchestrator(ai_bridge=bridge, executor=MagicMock())


def test_deep_subtask_timeouts_are_generous():
    orch = _orch("deep")
    # Deep mode: each role gets enough budget for real reasoning
    assert orch._configured_subtask_timeout("builder") == 7200   # 2h
    assert orch._configured_subtask_timeout("analyst") == 3600   # 1h
    assert orch._configured_subtask_timeout("planner") == 1800   # 30m
    assert orch._configured_subtask_timeout("reviewer") == 1800
    assert orch._configured_subtask_timeout("debugger") == 1800


def test_fast_subtask_timeouts_unchanged_by_v61():
    orch = _orch("fast")
    # Existing fast-mode caps preserved — V6.1 must not leak deep values here
    assert orch._configured_subtask_timeout("builder") == 600
    assert orch._configured_subtask_timeout("planner") == 300
    assert orch._configured_subtask_timeout("analyst") == 480


def test_deep_mode_builder_timeout_override_uplift():
    orch = _orch("deep")
    # Per-stream caps now allow long reasoning + slow-render builders
    assert orch._effective_builder_timeout("BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC") == 7200
    assert orch._effective_builder_timeout("BUILDER_FIRST_WRITE_TIMEOUT_SEC") == 1800
    assert orch._effective_builder_timeout("BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC") == 900
    assert orch._effective_builder_timeout("BUILDER_DIRECT_TEXT_NO_OUTPUT_TIMEOUT_SEC") == 600
    assert orch._effective_builder_timeout("BUILDER_DIRECT_TEXT_IDLE_TIMEOUT_SEC") == 900
    assert orch._effective_builder_timeout("BUILDER_DIRECT_TEXT_ACTIVE_STREAM_GRACE_SEC") == 600


def test_deep_max_ceiling_covers_retries():
    # default_timeout=7200 (builder) + 1 retry budget should fit under the hard
    # max_timeout ceiling without clamping
    orch = _orch("deep")
    assert orch._configured_subtask_timeout("builder") <= 10800


def test_user_config_override_still_wins_in_deep():
    # Explicit user config overrides deep defaults both directions
    bridge = AIBridge(config={"thinking_depth": "deep", "builder_timeout_sec": 1200})
    orch = Orchestrator(ai_bridge=bridge, executor=MagicMock())
    assert orch._configured_subtask_timeout("builder") == 1200
