import unittest
import asyncio
import tempfile
import time
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import orchestrator as orchestrator_module
from orchestrator import Orchestrator, Plan, SubTask, TaskStatus


class TestParseTestResult(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_json_fail_is_respected(self):
        output = '{"status":"fail","errors":["Missing <head> tag"]}'
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "fail")

    def test_strong_failure_marker_wins_over_pass_words(self):
        output = "Created successfully, but Missing <head> tag and No CSS styles found."
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "fail")

    def test_cloud_deploy_warning_is_not_a_real_failure(self):
        output = "No public URL available for deployment, but files exist in output directory."
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "pass")

    def test_deterministic_gate_pass_not_misparsed_by_failed_word(self):
        output = (
            "No failed assertions detected. Deterministic visual gate passed; smoke=pass; "
            "preview=http://127.0.0.1:8765/preview/index.html. __EVERMIND_TESTER_GATE__=PASS"
        )
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "pass")

    def test_no_artifact_failure_is_non_retryable(self):
        output = (
            "Deterministic visual gate failed; smoke=skipped; preview=n/a. "
            "QUALITY GATE FAILED: ['No HTML preview artifact found for tester validation'] "
            "__EVERMIND_TESTER_GATE__=FAIL"
        )
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "fail")
        self.assertFalse(parsed.get("retryable", True))


class TestReportIntegrity(unittest.TestCase):
    def test_report_not_success_when_pending_subtasks_exist(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="2", agent_type="tester", description="test", depends_on=["1"]),
            ],
        )
        plan.subtasks[0].status = TaskStatus.COMPLETED
        plan.subtasks[1].status = TaskStatus.PENDING

        report = orch._build_report(plan, results={"1": {"success": True}})
        self.assertFalse(report.get("success"))
        self.assertEqual(report.get("pending"), 1)


class TestBuilderQualityGate(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_rejects_tiny_incomplete_html(self):
        bad_html = "<!DOCTYPE html><html><body>hello</body></html>"
        report = self.orch._html_quality_report(bad_html, source="inline")
        self.assertFalse(report.get("pass"))
        self.assertGreater(len(report.get("errors", [])), 0)

    def test_accepts_polished_responsive_html(self):
        good_html = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
<title>Demo</title>
<style>
:root { --bg:#0b1020; --fg:#e9ecf1; --brand:#3dd5f3; --gap:16px; }
* { box-sizing:border-box; }
body { margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:var(--fg); background:linear-gradient(180deg,#0b1020,#121a34); }
header,main,section,footer,nav { display:block; }
nav { display:flex; justify-content:space-between; padding:20px; }
main { display:grid; gap:var(--gap); padding:24px; }
.hero { display:flex; gap:24px; align-items:center; min-height:40vh; }
.features { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.proof { display:grid; gap:10px; }
.cta { display:flex; gap:12px; }
button { padding:10px 16px; border-radius:10px; border:none; background:var(--brand); color:#001018; }
button:focus-visible { outline:2px solid #fff; outline-offset:2px; }
footer { padding:24px; opacity:.85; }
@media (max-width: 900px) { .hero { flex-direction:column; } .features { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><nav><strong>Brand</strong><a href=\"#\">Pricing</a></nav></header>
<main>
  <section class=\"hero\"><h1>Modern Product</h1><p>Ship fast with confidence.</p><button aria-label=\"Start trial\">Start free</button></section>
  <section class=\"features\"><article>Fast</article><article>Secure</article><article>Reliable</article></section>
  <section class=\"proof\"><blockquote>Trusted by teams.</blockquote></section>
  <section class=\"cta\"><button>Book demo</button></section>
</main>
<footer>2026 Demo Inc.</footer>
<script>document.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{}));</script>
</body>
</html>"""
        report = self.orch._html_quality_report(good_html, source="inline")
        self.assertTrue(report.get("pass"))
        self.assertGreaterEqual(report.get("score", 0), 70)

    def test_validate_builder_quality_uses_saved_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "index.html"
            p.write_text("<!DOCTYPE html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body><style>body{margin:0;}div{display:flex;}@media(max-width:800px){div{display:block;}}</style><header></header><main><section></section><section></section><footer></footer></main><script>1+1</script></body></html>", encoding="utf-8")
            report = self.orch._validate_builder_quality([str(p)], output="")
            # Can still fail quality score, but should read artifact and produce a structured report.
            self.assertIn("score", report)
            self.assertIn("errors", report)

    def test_rejects_emoji_icon_usage(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Emoji Icon Demo</title>
<style>
:root { --bg:#111827; --fg:#f8fafc; }
body { margin:0; background:var(--bg); color:var(--fg); font-family:sans-serif; }
header,main,section,footer,nav { display:block; }
main { display:grid; gap:16px; padding:24px; }
.hero { display:flex; gap:12px; align-items:center; }
.features { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
@media (max-width: 900px) { .features { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><nav>Brand</nav></header>
<main>
  <section class="hero"><h1>Product</h1><button>🚀 Start</button></section>
  <section class="features"><article>A</article><article>B</article><article>C</article></section>
  <section>Proof</section>
</main>
<footer>Footer</footer>
<script>console.log('ok')</script>
</body>
</html>"""
        report = self.orch._html_quality_report(html, source="inline")
        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Emoji glyphs detected" in err for err in report.get("errors", [])))

    def test_validate_builder_quality_auto_sanitizes_emoji_only_failure(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Emoji Auto Fix</title>
<style>
:root { --bg:#0b1020; --fg:#e9ecf1; --brand:#3dd5f3; --gap:16px; }
* { box-sizing:border-box; }
body { margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:var(--fg); background:linear-gradient(180deg,#0b1020,#121a34); }
header,main,section,footer,nav { display:block; }
nav { display:flex; justify-content:space-between; padding:20px; }
main { display:grid; gap:var(--gap); padding:24px; }
.hero { display:flex; gap:24px; align-items:center; min-height:40vh; }
.features { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.proof { display:grid; gap:10px; }
.cta { display:flex; gap:12px; }
button { padding:10px 16px; border-radius:10px; border:none; background:var(--brand); color:#001018; }
button:focus-visible { outline:2px solid #fff; outline-offset:2px; }
footer { padding:24px; opacity:.85; }
@media (max-width: 900px) { .hero { flex-direction:column; } .features { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><nav><strong>Brand</strong><a href="#">Pricing</a></nav></header>
<main>
  <section class="hero"><h1>Modern Product</h1><p>Ship fast with confidence.</p><button aria-label="Start trial">🚀 Start free</button></section>
  <section class="features"><article>Fast</article><article>Secure</article><article>Reliable</article></section>
  <section class="proof"><blockquote>Trusted by teams.</blockquote></section>
  <section class="cta"><button>Book demo</button></section>
</main>
<footer>2026 Demo Inc.</footer>
<script>document.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{}));</script>
</body>
</html>"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "index.html"
            p.write_text(html, encoding="utf-8")
            report = self.orch._validate_builder_quality([str(p)], output="")
            self.assertTrue(report.get("pass"))
            self.assertFalse(any("Emoji glyphs detected" in str(err) for err in report.get("errors", [])))
            self.assertTrue(any("Auto-sanitized emoji glyphs" in str(w) for w in report.get("warnings", [])))


class TestDifficultyPlansAndRetryTargets(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_standard_fallback_tester_depends_on_deployer(self):
        plan = self.orch._fallback_plan_for_difficulty("Build a page", "standard")
        tester = next(st for st in plan if st.agent_type == "tester")
        self.assertEqual(tester.depends_on, ["3"])

    def test_simple_fallback_tester_depends_on_deployer(self):
        plan = self.orch._fallback_plan_for_difficulty("Build a page", "simple")
        tester = next(st for st in plan if st.agent_type == "tester")
        self.assertEqual(tester.depends_on, ["2"])

    def test_collect_upstream_repair_targets_for_pro_chain(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = [
            type("Task", (), {"id": "1", "agent_type": "analyst", "depends_on": []})(),
            type("Task", (), {"id": "2", "agent_type": "builder", "depends_on": ["1"]})(),
            type("Task", (), {"id": "3", "agent_type": "reviewer", "depends_on": ["2"]})(),
            type("Task", (), {"id": "4", "agent_type": "debugger", "depends_on": ["3"]})(),
            type("Task", (), {"id": "5", "agent_type": "deployer", "depends_on": ["4"]})(),
            type("Task", (), {"id": "6", "agent_type": "tester", "depends_on": ["5"]})(),
        ]
        test_task = plan.subtasks[-1]
        targets = self.orch._collect_upstream_repair_targets(plan, test_task)
        target_types = [t.agent_type for t in targets]
        self.assertIn("debugger", target_types)
        self.assertIn("builder", target_types)

    def test_pro_prompt_requires_five_to_seven_subtasks(self):
        prompt = self.orch._planner_prompt_for_difficulty("pro")
        self.assertIn("7 subtasks", prompt)
        self.assertIn("MUST have 2 builders", prompt)

    def test_enforce_plan_shape_pro_canonicalizes_to_seven_nodes(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = [
            type("Task", (), {"id": "1", "agent_type": "analyst", "description": "Research UI patterns", "depends_on": []})(),
            type("Task", (), {"id": "2", "agent_type": "builder", "description": "Build page", "depends_on": ["1"]})(),
            type("Task", (), {"id": "3", "agent_type": "debugger", "description": "Fix bugs", "depends_on": ["2"]})(),
            type("Task", (), {"id": "4", "agent_type": "deployer", "description": "Deploy", "depends_on": ["3"]})(),
            type("Task", (), {"id": "5", "agent_type": "tester", "description": "Test", "depends_on": ["4"]})(),
        ]
        self.orch._enforce_plan_shape(plan, "Build landing page", "pro")
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[2].depends_on, ["1"])
        self.assertEqual(plan.subtasks[3].depends_on, ["2", "3"])
        self.assertEqual(plan.subtasks[4].depends_on, ["2", "3"])
        self.assertEqual(plan.subtasks[5].depends_on, ["4", "5"])
        self.assertEqual(plan.subtasks[6].depends_on, ["6"])

    def test_enforce_plan_shape_standard_canonicalizes_to_four_nodes(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = [
            type("Task", (), {"id": "1", "agent_type": "builder", "description": "Build page", "depends_on": []})(),
            type("Task", (), {"id": "2", "agent_type": "deployer", "description": "Deploy", "depends_on": ["1"]})(),
        ]
        self.orch._enforce_plan_shape(plan, "Build landing page", "standard")
        self.assertEqual([s.agent_type for s in plan.subtasks], ["builder", "reviewer", "deployer", "tester"])
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[2].depends_on, ["1"])
        self.assertEqual(plan.subtasks[3].depends_on, ["3"])

    def test_standard_shape_uses_task_adaptive_builder_for_game_goal(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = [
            type("Task", (), {"id": "1", "agent_type": "builder", "description": "Build a landing page hero section", "depends_on": []})(),
        ]
        self.orch._enforce_plan_shape(plan, "做一个贪吃蛇小游戏", "standard")
        self.assertIn("single-file HTML5 game", plan.subtasks[0].description)
        self.assertNotIn("hero section", plan.subtasks[0].description.lower())
        self.assertIn("Follow the design system", plan.subtasks[0].description)

    def test_pro_shape_game_focus_is_not_website_specific(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(plan, "Build a snake game", "pro")
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertIn("core gameplay", plan.subtasks[1].description.lower())
        self.assertNotIn("hero section", plan.subtasks[1].description.lower())
        self.assertIn("hud", plan.subtasks[2].description.lower())
        # New policy: all pro-mode task types run dual builders in parallel.
        self.assertEqual(plan.subtasks[2].depends_on, ["1"])

    def test_plan_fallback_pro_still_enforces_seven_nodes(self):
        class StubBridge:
            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": False, "error": "planner json parse failed"}

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)

        events = []

        async def _capture(evt):
            events.append(evt)

        orch.on_event = _capture
        plan = asyncio.run(orch._plan("Build a premium jewelry website", "kimi-coding", difficulty="pro"))
        self.assertEqual(len(plan.subtasks), 7)
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[2].depends_on, ["1"])  # website = parallel builders
        self.assertTrue(any(evt.get("type") == "planning_fallback" for evt in events))


class TestRuntimeQualityConfig(unittest.TestCase):
    def test_retry_policy_uses_runtime_config(self):
        bridge = type("Bridge", (), {"config": {"max_retries": 5}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        plan = type("PlanObj", (), {"subtasks": [], "max_total_retries": 10})()
        plan.subtasks = [
            type("Task", (), {"max_retries": 3})(),
            type("Task", (), {"max_retries": 3})(),
            type("Task", (), {"max_retries": 3})(),
        ]
        orch._apply_retry_policy(plan)
        self.assertTrue(all(st.max_retries == 5 for st in plan.subtasks))
        self.assertGreaterEqual(plan.max_total_retries, 15)

    def test_tester_smoke_reads_runtime_config(self):
        bridge = type("Bridge", (), {"config": {"tester_run_smoke": False}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        self.assertFalse(orch._configured_tester_smoke())

    def test_subtask_timeout_defaults_are_role_aware(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        self.assertEqual(orch._configured_subtask_timeout("builder"), 900)
        self.assertEqual(orch._configured_subtask_timeout("analyst"), 480)
        self.assertEqual(orch._configured_subtask_timeout("reviewer"), 420)
        self.assertEqual(orch._configured_subtask_timeout("tester"), 360)
        self.assertEqual(orch._configured_subtask_timeout("deployer"), 360)

    def test_retry_prompt_for_analyst_disables_browser(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="9", agent_type="analyst", description="research", depends_on=[])
        subtask.status = TaskStatus.FAILED
        subtask.error = "timeout"
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
        self.assertTrue(ok)
        self.assertIn("DO NOT USE BROWSER", captured.get("desc", ""))
        self.assertIn("under 500 words", captured.get("desc", ""))
        self.assertEqual(subtask.description, "research")

    def test_retry_prompt_for_other_nodes_asks_faster_execution(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="10", agent_type="reviewer", description="review", depends_on=[])
        subtask.status = TaskStatus.FAILED
        subtask.error = "latency spike"
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
        self.assertTrue(ok)
        self.assertIn("Be more careful and faster this time", captured.get("desc", ""))
        self.assertEqual(subtask.description, "review")


class TestWaitingAiProgressSignal(unittest.TestCase):
    def test_waiting_ai_event_hides_timeout_limit_fields(self):
        class SlowBridge:
            config = {}

            async def execute(self, **kwargs):
                await asyncio.sleep(10)
                return {"success": True, "output": "late success", "tool_results": []}

        events = []

        async def on_event(evt):
            events.append(evt)

        orch = Orchestrator(ai_bridge=SlowBridge(), executor=None, on_event=on_event)
        orch._configured_subtask_timeout = lambda agent_type: 1  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        plan = Plan(goal="Build test page", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("execution timeout after", str(result.get("error", "")))
        self.assertNotIn("limit", str(result.get("error", "")).lower())

        waiting_events = [
            evt for evt in events
            if evt.get("type") == "subtask_progress" and evt.get("stage") == "waiting_ai"
        ]
        self.assertTrue(waiting_events, "expected at least one waiting_ai progress event")
        self.assertNotIn("timeout_sec", waiting_events[0])


class TestFinalPreviewEmission(unittest.TestCase):
    def test_emit_final_preview_ignores_stale_artifacts(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            old_task = tmp_out / "task_1"
            old_task.mkdir(parents=True, exist_ok=True)
            old_html = old_task / "index.html"
            old_html.write_text("<!doctype html><html><head></head><body>old</body></html>", encoding="utf-8")

            # Simulate run start after old artifact was created.
            now = time.time()
            old_mtime = now - 120
            old_html.touch()
            old_html.chmod(0o644)
            os.utime(old_html, (old_mtime, old_mtime))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 0)

    def test_emit_final_preview_picks_run_local_artifact(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            task = tmp_out / "task_2"
            task.mkdir(parents=True, exist_ok=True)
            html = task / "index.html"
            html.write_text("<!doctype html><html><head></head><body>new</body></html>", encoding="utf-8")

            now = time.time()
            fresh = now + 1
            html.touch()
            html.chmod(0o644)
            os.utime(html, (fresh, fresh))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/task_2/index.html", preview_events[0].get("preview_url", ""))


class TestDependencyFailureBlocking(unittest.TestCase):
    def test_downstream_subtasks_are_blocked_when_builder_fails(self):
        class StubBridge:
            def __init__(self):
                self.calls = []
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.calls.append(node.get("type"))
                if node.get("type") == "builder":
                    return {"success": False, "output": "", "error": ""}
                return {"success": True, "output": "ok", "tool_results": []}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=0),
                SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"]),
                SubTask(id="3", agent_type="deployer", description="deploy", depends_on=["1"]),
                SubTask(id="4", agent_type="tester", description="test", depends_on=["3"]),
            ],
        )

        asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertEqual(bridge.calls, ["builder"])
        reviewer = next(st for st in plan.subtasks if st.id == "2")
        deployer = next(st for st in plan.subtasks if st.id == "3")
        tester = next(st for st in plan.subtasks if st.id == "4")
        self.assertEqual(reviewer.status, TaskStatus.FAILED)
        self.assertEqual(deployer.status, TaskStatus.FAILED)
        self.assertEqual(tester.status, TaskStatus.FAILED)
        self.assertIn("Blocked by failed dependencies", reviewer.error)


class TestRetryFromFailureStateReset(unittest.TestCase):
    def test_retry_from_failure_requeues_downstream_tasks(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "<!DOCTYPE html><html><head></head><body>fixed</body></html>"}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="2", agent_type="deployer", description="deploy", depends_on=["1"]),
                SubTask(id="3", agent_type="tester", description="test", depends_on=["2"]),
            ],
        )
        for st in plan.subtasks:
            st.status = TaskStatus.COMPLETED
            st.output = "ok"

        test_task = plan.subtasks[2]
        results = {
            "1": {"success": True, "output": "builder-old"},
            "2": {"success": True, "output": "deployer-old"},
            "3": {"success": True, "output": "tester-old"},
        }
        succeeded = {"1", "2", "3"}
        completed = {"1", "2", "3"}
        failed = set()

        async def _fake_execute(subtask, _plan, _model, _results):
            subtask.status = TaskStatus.COMPLETED
            subtask.output = "fixed"
            subtask.error = ""
            return {"success": True, "output": "fixed"}

        with patch.object(orch, "_execute_subtask", new=AsyncMock(side_effect=_fake_execute)):
            asyncio.run(
                orch._retry_from_failure(
                    plan=plan,
                    test_task=test_task,
                    test_result={"status": "fail", "errors": ["visual gate failed"], "suggestion": "fix"},
                    model="kimi-coding",
                    results=results,
                    succeeded=succeeded,
                    completed=completed,
                    failed=failed,
                )
            )

        builder = plan.subtasks[0]
        deployer = plan.subtasks[1]
        tester = plan.subtasks[2]

        self.assertEqual(builder.status, TaskStatus.COMPLETED)
        self.assertGreaterEqual(builder.retries, 1)
        self.assertEqual(deployer.status, TaskStatus.PENDING)
        self.assertEqual(tester.status, TaskStatus.PENDING)
        self.assertNotIn("2", succeeded)
        self.assertNotIn("3", succeeded)
        self.assertNotIn("2", completed)
        self.assertNotIn("3", completed)
        self.assertNotIn("2", results)
        self.assertNotIn("3", results)


class TestHandleFailureRequiresValidation(unittest.TestCase):
    def test_builder_retry_cannot_succeed_on_text_only_output(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                # Pretend LLM call succeeded but did not write any file.
                return {"success": True, "output": "brief summary only", "tool_results": []}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(
            goal="test",
            subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=1)],
        )
        builder = plan.subtasks[0]
        builder.error = "initial quality failure"

        ok = asyncio.run(orch._handle_failure(builder, plan, "kimi-coding", results={}))
        self.assertFalse(ok)
        self.assertEqual(builder.status, TaskStatus.FAILED)
        self.assertIn("quality gate failed", builder.error.lower())


class TestTesterVisualGate(unittest.TestCase):
    def test_tester_visual_gate_supports_root_level_artifact(self):
        bridge = type("Bridge", (), {"config": {"tester_run_smoke": False}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>ok</body></html>",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch(
                    "orchestrator.validate_preview",
                    new=AsyncMock(
                        return_value={
                            "ok": True,
                            "errors": [],
                            "warnings": [],
                            "preview_url": "http://127.0.0.1:8765/preview/index.html",
                            "smoke": {"status": "skipped"},
                        }
                    ),
                ):
                    result = asyncio.run(orch._run_tester_visual_gate())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(result.get("task_id"), "root")
        self.assertTrue(result.get("ok"))

    def test_tester_visual_gate_fallbacks_to_root_index_when_lookup_misses(self):
        bridge = type("Bridge", (), {"config": {"tester_run_smoke": False}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>ok</body></html>",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch("orchestrator.latest_preview_artifact", return_value=(None, None)):
                    with patch(
                        "orchestrator.validate_preview",
                        new=AsyncMock(
                            return_value={
                                "ok": True,
                                "errors": [],
                                "warnings": [],
                                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                                "smoke": {"status": "skipped"},
                            }
                        ),
                    ):
                        result = asyncio.run(orch._run_tester_visual_gate())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(result.get("task_id"), "root")
        self.assertTrue(result.get("ok"))


class TestReviewerVisualGate(unittest.TestCase):
    def test_reviewer_fails_if_browser_tool_not_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "review complete", "tool_results": [], "tool_call_stats": {}}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("Reviewer visual gate failed", str(result.get("error", "")))

    def test_reviewer_passes_when_browser_tool_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"status\":\"approved\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 1},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_pro_reviewer_rejected_requests_builder_requeue_once(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.9,\"improvements\":[\"Improve spacing\"]}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 1},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="test", difficulty="pro", subtasks=[builder, reviewer])

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("requeue_requested"))
        self.assertEqual(result.get("requeue_subtasks"), ["1", "2"])
        self.assertEqual(builder.status, TaskStatus.PENDING)
        self.assertEqual(builder.retries, 1)
        self.assertEqual(reviewer.status, TaskStatus.PENDING)

    def test_pro_reviewer_rejected_skips_requeue_when_budget_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.9}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 1},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch._pro_reviewer_requeues = 1

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="test", difficulty="pro", subtasks=[builder, reviewer])

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("requeue_requested", False))
        self.assertTrue(result.get("success"))


class TestTesterBrowserGate(unittest.TestCase):
    def test_tester_fails_if_browser_tool_not_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "{\"status\":\"pass\"}", "tool_results": [], "tool_call_stats": {}}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("Tester visual gate failed", str(result.get("error", "")))

    def test_tester_passes_when_browser_tool_used_and_gate_passes(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 2},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        with patch("orchestrator.validate_preview", new=AsyncMock(return_value={
            "ok": True,
            "errors": [],
            "warnings": [],
            "preview_url": "http://127.0.0.1:8765/preview/index.html",
            "smoke": {"status": "skipped"},
        })):
            with patch("orchestrator.latest_preview_artifact", return_value=("root", Path("/tmp/evermind_output/index.html"))):
                with patch("orchestrator.build_preview_url_for_file", return_value="http://127.0.0.1:8765/preview/index.html"):
                    result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))


class TestTesterNonRetryableFailure(unittest.TestCase):
    def test_non_retryable_tester_failure_does_not_requeue_builder(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 1},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="9", agent_type="tester", description="test", depends_on=[])])

        with patch.object(
            orch,
            "_run_tester_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": False,
                    "errors": ["No HTML preview artifact found for tester validation"],
                    "warnings": [],
                    "preview_url": None,
                    "smoke": {"status": "skipped", "reason": "no_artifact"},
                }
            ),
        ):
            with patch.object(orch, "_retry_from_failure", new=AsyncMock()) as retry_mock:
                asyncio.run(orch._execute_plan(plan, "kimi-coding"))
                retry_mock.assert_not_called()

        tester = plan.subtasks[0]
        self.assertEqual(tester.status, TaskStatus.FAILED)
        self.assertIn("No HTML preview artifact found", tester.error)


if __name__ == "__main__":
    unittest.main()
