import unittest
import asyncio
import tempfile
import time
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import orchestrator as orchestrator_module
import preview_validation as preview_validation_module
from orchestrator import Orchestrator, Plan, SubTask, TaskStatus


class TestParseTestResult(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_json_fail_is_respected(self):
        output = '{"status":"fail","errors":["Missing <head> tag"]}'
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "fail")


class TestReviewerGate(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_reviewer_description_uses_consistent_strict_thresholds(self):
        desc = self.orch._reviewer_task_description("做一个高质量官网", pro=True)
        self.assertIn("ANY single dimension score below 5 = AUTOMATIC REJECT", desc)
        self.assertIn("originality", desc)
        self.assertIn("ship_readiness", desc)
        self.assertIn("missing_deliverables", desc)

    def test_parse_reviewer_verdict_rejects_when_blocking_issues_exist(self):
        output = (
            '{"verdict":"APPROVED","scores":{"layout":8,"color":8,"typography":8,"animation":7,'
            '"responsive":8,"functionality":8,"completeness":8,"originality":7},'
            '"blocking_issues":["Primary CTA is broken"],"required_changes":[]}'
        )
        self.assertEqual(self.orch._parse_reviewer_verdict(output), "REJECTED")

    def test_parse_reviewer_verdict_rejects_when_ship_readiness_low_or_missing_deliverables(self):
        output = (
            '{"verdict":"APPROVED","ship_readiness":6,'
            '"scores":{"layout":8,"color":8,"typography":8,"animation":7,"responsive":8,"functionality":8,"completeness":8,"originality":7},'
            '"blocking_issues":[],"required_changes":[],"missing_deliverables":["game over loop"]}'
        )
        self.assertEqual(self.orch._parse_reviewer_verdict(output), "REJECTED")

    def test_parse_reviewer_verdict_rejects_when_core_quality_dimensions_below_six(self):
        output = (
            '{"verdict":"APPROVED","ship_readiness":8,'
            '"scores":{"layout":8,"color":8,"typography":8,"animation":7,"responsive":8,"functionality":5,"completeness":8,"originality":7},'
            '"blocking_issues":[],"required_changes":[],"missing_deliverables":[]}'
        )
        self.assertEqual(self.orch._parse_reviewer_verdict(output), "REJECTED")

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

    def test_report_aggregates_token_and_cost_from_results(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="2", agent_type="tester", description="test", depends_on=["1"]),
            ],
        )
        plan.subtasks[0].status = TaskStatus.COMPLETED
        plan.subtasks[1].status = TaskStatus.COMPLETED

        report = orch._build_report(plan, results={
            "1": {"success": True, "tokens_used": 1200, "cost": 0.42},
            "2": {"success": True, "tokens_used": 300, "cost": 0.08},
        })

        self.assertEqual(report.get("total_tokens"), 1500)
        self.assertAlmostEqual(report.get("total_cost"), 0.5, places=6)


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

    def test_validate_builder_quality_allows_partial_artifact_until_pair_ready(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            part = tmp_out / "index_part1.html"
            part.write_text("<section><h1>Top Half</h1></section>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality([str(part)], output="")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(report.get("pass"))
            self.assertTrue(any("Partial builder artifact" in w for w in report.get("warnings", [])))

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
        prompt = self.orch._planner_prompt_for_difficulty("Build landing page", "pro")
        self.assertIn("7 subtasks", prompt)
        self.assertIn("MUST have 2 builders", prompt)

    def test_game_planner_prompt_blocks_playable_game_research(self):
        prompt = self.orch._planner_prompt_for_difficulty("做一个超级马里奥风格平台跳跃游戏", "standard")
        self.assertIn("do NOT send analyst to spend time playing browser games", prompt)

    def test_standard_asset_heavy_game_adds_specialized_pipeline_when_image_backend_available(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        }), executor=None)
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        orch._enforce_plan_shape(plan, "做一个像素风平台跳跃游戏，包含角色素材和 spritesheet", "standard")
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "imagegen", "spritesheet", "assetimport", "builder", "reviewer", "deployer", "tester"],
        )
        self.assertEqual(plan.subtasks[4].depends_on, ["1", "4"])

    def test_standard_asset_heavy_game_skips_specialized_pipeline_without_image_backend(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(plan, "做一个像素风平台跳跃游戏，包含角色素材和 spritesheet", "standard")
        self.assertEqual([s.agent_type for s in plan.subtasks], ["builder", "reviewer", "deployer", "tester"])
        self.assertIn("No configured image-generation backend is available", plan.subtasks[0].description)

    def test_browser_snapshot_log_line_includes_ref_preview(self):
        line = self.orch._browser_action_log_line({
            "action": "snapshot",
            "snapshot_ref_count": 6,
            "snapshot_refs_preview": [
                {"ref": "ref-1", "label": "Start Game", "role": "button"},
                {"ref": "ref-2", "label": "Mute", "role": "button"},
            ],
        })
        self.assertIn("ref-1", line or "")
        self.assertIn("Start Game", line or "")
        self.assertIn("等 6 个", line or "")

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

    def test_validate_analyst_handoff_requires_role_specific_tags(self):
        plan = Plan(
            goal="做一个高端 AI 官网",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="builder", description="top", depends_on=["1"]),
                SubTask(id="3", agent_type="builder", description="bottom", depends_on=["1"]),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"]),
                SubTask(id="5", agent_type="tester", description="test", depends_on=["4"]),
                SubTask(id="6", agent_type="debugger", description="debug", depends_on=["5"]),
            ],
        )
        text = (
            "<reference_sites>https://example.com</reference_sites>\n"
            "<design_direction>premium dark saas</design_direction>\n"
            "<non_negotiables>no emoji</non_negotiables>\n"
            "<deliverables_contract>hero, nav, proof, pricing, footer</deliverables_contract>\n"
            "<risk_register>generic hierarchy, weak CTA</risk_register>\n"
            "<builder_1_handoff>hero + nav</builder_1_handoff>\n"
            "<reviewer_handoff>be strict</reviewer_handoff>\n"
        )
        missing = self.orch._validate_analyst_handoff(text, plan)
        self.assertIn("builder_2_handoff", missing)
        self.assertIn("tester_handoff", missing)
        self.assertIn("debugger_handoff", missing)

    def test_build_analyst_handoff_context_uses_builder_slot(self):
        plan = Plan(
            goal="做一个高端 AI 官网",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="builder", description="top", depends_on=["1"]),
                SubTask(id="3", agent_type="builder", description="bottom", depends_on=["1"]),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"]),
            ],
        )
        analyst_output = (
            "<reference_sites>https://a.com\nhttps://b.com</reference_sites>\n"
            "<design_direction>premium, quiet luxury, strong hierarchy</design_direction>\n"
            "<non_negotiables>no emoji icons</non_negotiables>\n"
            "<deliverables_contract>premium hero, trust layer, proof, pricing, footer</deliverables_contract>\n"
            "<risk_register>cheap iconography, weak spacing, empty footer</risk_register>\n"
            "<builder_1_handoff>handle header, hero, benefits</builder_1_handoff>\n"
            "<builder_2_handoff>handle proof, pricing, footer</builder_2_handoff>\n"
            "<reviewer_handoff>reject generic spacing and emoji glyphs</reviewer_handoff>\n"
        )
        context = self.orch._build_analyst_handoff_context(plan, plan.subtasks[2], analyst_output)
        self.assertIn("Builder 2 Handoff", context)
        self.assertIn("handle proof, pricing, footer", context)
        self.assertIn("no emoji icons", context)
        self.assertIn("Deliverables Contract", context)
        self.assertIn("Risk Register", context)


class TestCustomPlanPreservation(unittest.TestCase):
    def test_run_preserves_custom_plan_task_text(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None)
        orch.emit = AsyncMock()
        orch._execute_plan = AsyncMock(return_value={})
        orch._emit_final_preview = AsyncMock()
        orch._build_report = MagicMock(return_value={"success": True, "subtasks": []})

        canonical_context = {
            "task_id": "task_1",
            "run_id": "run_1",
            "is_custom_plan": True,
            "node_executions": [
                {
                    "id": "ne_1",
                    "node_key": "planner",
                    "node_label": "Planner",
                    "input_summary": "拆解执行顺序并输出骨架计划",
                    "depends_on_keys": [],
                },
                {
                    "id": "ne_2",
                    "node_key": "analyst",
                    "node_label": "Analyst",
                    "input_summary": "研究 3 个竞品并写下游任务书",
                    "depends_on_keys": ["planner"],
                },
            ],
        }

        result = asyncio.run(orch.run(
            goal="做一个高端 AI 官网",
            canonical_context=canonical_context,
        ))

        self.assertTrue(result["success"])
        self.assertIsNotNone(orch.active_plan)
        self.assertEqual(orch.active_plan.subtasks[0].description, "拆解执行顺序并输出骨架计划")
        self.assertEqual(orch.active_plan.subtasks[1].description, "研究 3 个竞品并写下游任务书")


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


class TestCanonicalBroadcastRuntimeModule(unittest.IsolatedAsyncioTestCase):
    async def test_sync_ne_status_uses_live_main_module_for_broadcasts(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._canonical_ctx = {"task_id": "task_1", "run_id": "run_1"}
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        fake_ne_store = MagicMock()
        fake_ne_store.get_node_execution.return_value = {
            "id": "nodeexec_1",
            "node_key": "planner",
            "node_label": "planner",
            "status": "running",
            "assigned_model": "kimi-coding",
            "assigned_provider": "",
            "retry_count": 0,
            "tokens_used": 0,
            "cost": 0.0,
            "input_summary": "planner task",
            "output_summary": "",
            "error_message": "",
            "artifact_ids": [],
            "started_at": 1.0,
            "ended_at": 0.0,
            "created_at": 1.0,
            "progress": 5,
            "phase": "",
            "version": 3,
        }
        fake_run_store = MagicMock()
        fake_run_store.get_run.return_value = {
            "id": "run_1",
            "active_node_execution_ids": ["nodeexec_1"],
            "version": 7,
        }
        fake_main = SimpleNamespace(
            _broadcast_ws_event=AsyncMock(),
            _transition_node_if_needed=MagicMock(return_value=True),
        )

        with patch.dict(sys.modules, {"__main__": fake_main}, clear=False):
            with patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                with patch.object(orchestrator_module, "get_run_store", return_value=fake_run_store):
                    await orch._sync_ne_status("1", "running", input_summary="planner task")

        fake_main._transition_node_if_needed.assert_called_once_with("nodeexec_1", "running")
        fake_main._broadcast_ws_event.assert_awaited()

    async def test_emit_ne_progress_uses_live_main_module_for_broadcasts(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._canonical_ctx = {"task_id": "task_1", "run_id": "run_1"}
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        fake_ne_store = MagicMock()
        fake_ne_store.get_node_execution.return_value = {
            "id": "nodeexec_1",
            "progress": 42,
            "phase": "processing",
            "version": 5,
        }
        fake_run_store = MagicMock()
        fake_run_store.get_run.return_value = {"id": "run_1", "version": 9}
        fake_main = SimpleNamespace(
            _broadcast_ws_event=AsyncMock(),
            _transition_node_if_needed=MagicMock(return_value=True),
        )

        with patch.dict(sys.modules, {"__main__": fake_main}, clear=False):
            with patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                with patch.object(orchestrator_module, "get_run_store", return_value=fake_run_store):
                    await orch._emit_ne_progress("1", progress=42, phase="processing", partial_output="hello")

        fake_main._broadcast_ws_event.assert_awaited()
        payload = fake_main._broadcast_ws_event.await_args.args[0]["payload"]
        self.assertEqual(payload["nodeExecutionId"], "nodeexec_1")
        self.assertEqual(payload["progress"], 42)
        self.assertEqual(payload["phase"], "processing")

    def test_timeout_preserves_model_partial_output_for_retry(self):
        class SlowBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": "<!DOCTYPE html>\n<html><body>" + ("partial-content-" * 20),
                })
                await asyncio.sleep(10)
                return {"success": True, "output": "late success", "tool_results": []}

        orch = Orchestrator(ai_bridge=SlowBridge(), executor=None)
        orch._configured_subtask_timeout = lambda agent_type: 1  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="partial-timeout", agent_type="builder", description="build", depends_on=[])
        plan = Plan(goal="Build test page", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("<!DOCTYPE html>", str(result.get("output", "")))
        self.assertIn("partial-content", subtask.last_partial_output)


class TestTimeoutContinuationRecovery(unittest.TestCase):
    def test_builder_timeout_retry_uses_saved_partial_output(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="11", agent_type="builder", description="build landing page", depends_on=[])
        subtask.status = TaskStatus.FAILED
        subtask.error = "timeout after 120s"
        subtask.last_partial_output = "<!DOCTYPE html>\n<html>\n" + ("component-block\n" * 20)
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
        self.assertTrue(ok)
        self.assertIn("上次执行因超时中断", captured.get("desc", ""))
        self.assertIn("_partial_builder.html", captured.get("desc", ""))
        self.assertNotIn("<!DOCTYPE html>", captured.get("desc", ""))
        self.assertEqual(subtask.description, "build landing page")


class TestExtractionAndRetrySemantics(unittest.TestCase):
    def test_extract_and_save_code_auto_closes_fenced_html(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>Demo</title></head>
<body><main>hello</main>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "fenced")
                saved = (Path(td) / "task_fenced" / "index.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any(str(p).endswith("index.html") for p in files))
        self.assertIn("</body>", saved.lower())
        self.assertIn("</html>", saved.lower())

    def test_extract_and_save_code_auto_closes_raw_html(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """prefix
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Raw</title></head>
<body><section>demo</section>"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "raw")
                saved = (Path(td) / "task_raw" / "index.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any(str(p).endswith("index.html") for p in files))
        self.assertIn("</body>", saved.lower())
        self.assertIn("</html>", saved.lower())

    def test_execute_subtask_exception_does_not_force_failed_status_before_retry_handler(self):
        class CrashBridge:
            config = {}

            async def execute(self, **kwargs):
                raise RuntimeError("bridge exploded")

        orch = Orchestrator(ai_bridge=CrashBridge(), executor=None)
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch.emit = AsyncMock()
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=2)
        plan = Plan(goal="Build test page", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertNotIn("failed", statuses)

    def test_handle_failure_retries_without_terminal_failed_status(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="builder", description="build landing page", depends_on=[], max_retries=2)
        subtask.status = TaskStatus.FAILED
        subtask.error = "builder quality gate failed"
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        async def fake_execute_subtask(st, _plan, _model, _results):
            st.status = TaskStatus.COMPLETED
            st.output = "ok"
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertEqual(statuses[0], "running")
        self.assertNotIn("failed", statuses)

    def test_handle_failure_marks_failed_only_after_retry_budget_exhausted(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="builder", description="build landing page", depends_on=[], max_retries=1)
        subtask.status = TaskStatus.FAILED
        subtask.error = "builder quality gate failed"
        subtask.retries = 1
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertFalse(ok)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertEqual(statuses, ["failed"])

    def test_analyst_gate_requires_two_live_reference_urls(self):
        """Analyst gate is now a soft warning — output still passes."""
        class StubBridge:
            config = {}

            async def execute(self, **kwargs):
                return {
                    "success": True,
                    "output": "A short design brief with no URLs.",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[], max_retries=1)
        plan = Plan(goal="Research SaaS references", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        # Analyst gate is now a soft warning, so result should still succeed
        self.assertTrue(result.get("success"))

    def test_analyst_output_injects_browser_urls_into_report(self):
        class StubBridge:
            config = {}

            async def execute(self, **kwargs):
                return {
                    "success": True,
                    "output": (
                        "<reference_sites>\n"
                        "- https://example.com\n"
                        "- https://example.org\n"
                        "</reference_sites>\n"
                        "<design_direction>Color palette is cool blue. Layout is hero-first.</design_direction>\n"
                        "<non_negotiables>No emoji glyphs.</non_negotiables>\n"
                    ),
                    "tool_results": [
                        {"success": True, "data": {"url": "https://example.com"}},
                        {"success": True, "data": {"url": "https://example.org"}},
                    ],
                    "tool_call_stats": {"browser": 2},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[], max_retries=1)
        plan = Plan(goal="Research SaaS references", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        output = str(result.get("output", ""))
        self.assertIn("<reference_sites>", output)
        self.assertIn("https://example.com", output)
        self.assertIn("https://example.org", output)

    def test_analyst_retry_after_reference_gate_failure_forces_browser_usage(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[], max_retries=2)
        subtask.status = TaskStatus.FAILED
        subtask.error = "Analyst research incomplete: must browse at least 2 live reference URLs and list them in the report."
        plan = Plan(goal="Research SaaS references", subtasks=[subtask])
        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        self.assertIn("MUST use the browser tool on at least 2 different live URLs", captured.get("desc", ""))
        self.assertIn("<reference_sites>", captured.get("desc", ""))


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

    def test_emit_final_preview_materializes_parallel_builder_parts(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            part1 = tmp_out / "index_part1.html"
            part2 = tmp_out / "index_part2.html"
            part1.write_text(
                "<!doctype html><html><head><title>Split Demo</title><style>body{margin:0}header{display:block}</style></head><body><header>Top</header><main><section>Hero</section></main><script>window.topHalf=true;</script></body></html>",
                encoding="utf-8",
            )
            part2.write_text(
                "<!doctype html><html><head><style>footer{display:block}.pricing{display:grid}@media(max-width:700px){.pricing{display:block}}</style></head><body><section class='pricing'>Pricing</section><footer>Footer</footer><script>window.bottomHalf=true;</script></body></html>",
                encoding="utf-8",
            )

            now = time.time()
            fresh = now + 1
            os.utime(part1, (fresh, fresh))
            os.utime(part2, (fresh, fresh))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/index.html", preview_events[0].get("preview_url", ""))


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

    def test_parallel_website_builder_failure_degrades_without_preview(self):
        """When one builder fails but another succeeds, downstream nodes proceed
        even if preview artifact is not yet on disk (to avoid stalling the run)."""
        class StubBridge:
            def __init__(self, out_dir: Path):
                self.calls = []
                self.config = {}
                self.out_dir = out_dir

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.calls.append(node.get("type"))
                text = str(input_data)
                if node.get("type") == "builder" and "index_part1.html" in text:
                    part1 = self.out_dir / "index_part1.html"
                    part1.write_text("<section><h1>Top Half</h1></section>", encoding="utf-8")
                    return {
                        "success": True,
                        "output": "<section><h1>Top Half</h1></section>",
                        "tool_results": [{"written": True, "path": str(part1)}],
                    }
                if node.get("type") == "builder" and "index_part2.html" in text:
                    return {"success": False, "output": "", "error": "builder part2 failed", "tool_results": []}
                # Reviewer/deployer/tester should now be called
                return {"success": True, "output": "ok", "tool_results": []}

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            bridge = StubBridge(out_dir)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch._current_task_type = "website"

            async def _noop(_evt):
                return None

            orch.on_event = _noop
            plan = Plan(
                goal="build premium website",
                subtasks=[
                    SubTask(id="2", agent_type="builder", description="Build top and save to /tmp/evermind_output/index_part1.html", depends_on=[], max_retries=0),
                    SubTask(id="3", agent_type="builder", description="Build bottom and save to /tmp/evermind_output/index_part2.html", depends_on=[], max_retries=0),
                    SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"]),
                    SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["2", "3"]),
                    SubTask(id="6", agent_type="tester", description="test", depends_on=["4", "5"]),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                preview_validation_module.OUTPUT_DIR = out_dir
                asyncio.run(orch._execute_plan(plan, "kimi-coding"))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        # Downstream nodes should now PROCEED (no longer blocked)
        self.assertIn("reviewer", bridge.calls)
        self.assertIn("deployer", bridge.calls)

    def test_parallel_nonwebsite_builder_failure_can_degrade_with_preview(self):
        class StubBridge:
            def __init__(self, out_dir: Path):
                self.calls = []
                self.config = {"tester_run_smoke": False}
                self.out_dir = out_dir

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.calls.append(node.get("type"))
                text = str(input_data)
                if node.get("type") == "builder" and "Build core gameplay" in text:
                    index = self.out_dir / "index.html"
                    index.write_text(
                        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Snake Arena</title>
<style>
:root { --bg:#071018; --panel:#102132; --fg:#eff6ff; --accent:#38bdf8; --accent2:#34d399; --danger:#fb7185; }
* { box-sizing:border-box; }
body { margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:radial-gradient(circle at top,#12324d,#071018 62%); color:var(--fg); }
header,main,section,footer,nav { display:block; }
nav { display:flex; justify-content:space-between; align-items:center; padding:18px 24px; }
main { display:grid; gap:18px; padding:24px; }
.hero { display:grid; grid-template-columns:1.2fr .8fr; gap:18px; align-items:center; }
.panel { background:rgba(16,33,50,.88); border:1px solid rgba(148,163,184,.18); border-radius:18px; padding:18px; box-shadow:0 18px 60px rgba(0,0,0,.28); }
.hud { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.arena { min-height:320px; border-radius:16px; border:1px solid rgba(56,189,248,.35); background:linear-gradient(180deg,rgba(15,23,42,.86),rgba(8,47,73,.9)); }
.tips { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.cta { display:flex; gap:12px; flex-wrap:wrap; }
button { border:none; border-radius:999px; padding:12px 18px; font-weight:700; background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#062033; }
small { opacity:.75; }
@media (max-width: 900px) { .hero { grid-template-columns:1fr; } .hud, .tips { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><nav><strong>Snake Arena</strong><small>Core gameplay ready</small></nav></header>
<main>
  <section class="hero">
    <div class="panel">
      <h1>Arcade snake with polished movement</h1>
      <p>Core loop, input handling, collision detection, and board rendering are already wired.</p>
      <div class="cta"><button>Start Run</button><button>Practice Mode</button></div>
    </div>
    <div class="panel hud">
      <article><strong>Score</strong><p>0012</p></article>
      <article><strong>Speed</strong><p>Normal</p></article>
      <article><strong>Lives</strong><p>3</p></article>
    </div>
  </section>
  <section class="panel arena"><canvas aria-label="Game arena"></canvas></section>
  <section class="tips">
    <article class="panel">Arrow key controls</article>
    <article class="panel">Fruit combo streaks</article>
    <article class="panel">Pause and restart states</article>
  </section>
</main>
<footer class="panel">Ready for polish pass and secondary effects.</footer>
<script>window.game=true; window.snakeReady=true;</script>
</body>
</html>""",
                        encoding="utf-8",
                    )
                    return {
                        "success": True,
                        "output": "<!DOCTYPE html><html><body>game</body></html>",
                        "tool_results": [{"written": True, "path": str(index)}],
                    }
                if node.get("type") == "builder":
                    return {"success": False, "output": "", "error": "polish builder failed", "tool_results": []}
                if node.get("type") == "reviewer":
                    return {
                        "success": True,
                        "output": "{\"verdict\":\"APPROVED\"}",
                        "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                        "tool_call_stats": {"browser": 1},
                    }
                if node.get("type") == "deployer":
                    return {"success": True, "output": "{\"status\":\"deployed\",\"preview_url\":\"http://127.0.0.1:8765/preview/index.html\"}", "tool_results": []}
                if node.get("type") == "tester":
                    return {
                        "success": True,
                        "output": "{\"status\":\"pass\"}",
                        "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                        "tool_call_stats": {"browser": 1},
                    }
                return {"success": True, "output": "ok", "tool_results": []}

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            bridge = StubBridge(out_dir)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch._current_task_type = "game"

            async def _noop(_evt):
                return None

            orch.on_event = _noop
            plan = Plan(
                goal="build snake game",
                subtasks=[
                    SubTask(id="2", agent_type="builder", description="ADVANCED MODE\nBuild core gameplay and save to /tmp/evermind_output/index.html", depends_on=[], max_retries=0),
                    SubTask(id="3", agent_type="builder", description="ADVANCED MODE\nBuild polish layer", depends_on=[], max_retries=0),
                    SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"]),
                    SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["2", "3"]),
                    SubTask(id="6", agent_type="tester", description="test", depends_on=["4", "5"]),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                preview_validation_module.OUTPUT_DIR = out_dir
                asyncio.run(orch._execute_plan(plan, "kimi-coding"))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertEqual(bridge.calls, ["builder", "builder", "reviewer", "deployer", "tester"])
        reviewer = next(st for st in plan.subtasks if st.id == "4")
        deployer = next(st for st in plan.subtasks if st.id == "5")
        tester = next(st for st in plan.subtasks if st.id == "6")
        self.assertEqual(reviewer.status, TaskStatus.COMPLETED)
        self.assertEqual(deployer.status, TaskStatus.COMPLETED)
        self.assertEqual(tester.status, TaskStatus.COMPLETED)


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
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"status\":\"approved\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
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

    def test_reviewer_passes_with_observe_and_act_browser_flow(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "act", "subaction": "click", "ok": True, "target": "ref-1 Start", "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"status\":\"approved\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
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

    def test_standard_reviewer_rejected_requests_builder_requeue(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.9,\"improvements\":[\"Improve spacing\"]}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="test", difficulty="standard", subtasks=[builder, reviewer])

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("requeue_requested"))
        self.assertEqual(result.get("requeue_subtasks"), ["1", "2"])
        self.assertEqual(builder.status, TaskStatus.PENDING)
        self.assertEqual(builder.retries, 1)
        self.assertEqual(reviewer.status, TaskStatus.PENDING)

    def test_pro_reviewer_rejected_requeues_all_upstream_builders(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.1,\"improvements\":[\"Improve layout\"]}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder_a = SubTask(id="1", agent_type="builder", description="build top", depends_on=[])
        builder_b = SubTask(id="2", agent_type="builder", description="build bottom", depends_on=[])
        builder_a.status = TaskStatus.COMPLETED
        builder_b.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="4", agent_type="reviewer", description="review", depends_on=["1", "2"])
        plan = Plan(goal="test", difficulty="pro", subtasks=[builder_a, builder_b, reviewer])

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("requeue_requested"))
        self.assertEqual(result.get("requeue_subtasks"), ["1", "2", "4"])
        self.assertEqual(builder_a.status, TaskStatus.PENDING)
        self.assertEqual(builder_b.status, TaskStatus.PENDING)
        self.assertEqual(builder_a.retries, 1)
        self.assertEqual(builder_b.retries, 1)

    def test_pro_reviewer_rejected_skips_requeue_when_budget_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_max_rejections": 1}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.9}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch._reviewer_requeues = 1

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

    def test_reviewer_game_requires_keyboard_press(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "snapshot",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                })
                await on_progress({
                    "stage": "browser_action",
                    "action": "click",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                })
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 2},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("multiple gameplay key inputs", str(result.get("error", "")))

    def test_reviewer_website_requires_post_interaction_verification(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap111",
                    },
                    {
                        "stage": "browser_action",
                        "action": "scroll",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "scroll222",
                        "state_changed": True,
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "target": "text=Get Started",
                        "state_hash": "scroll222",
                        "previous_state_hash": "scroll222",
                        "state_changed": False,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 3},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个产品官网", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("post-click state", str(result.get("error", "")))

    def test_reviewer_fails_on_failed_requests(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap111",
                        "failed_request_count": 3,
                    },
                    {
                        "stage": "browser_action",
                        "action": "scroll",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "scroll222",
                        "state_changed": True,
                        "failed_request_count": 3,
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "click333",
                        "previous_state_hash": "scroll222",
                        "state_changed": True,
                        "failed_request_count": 3,
                    },
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "click333",
                        "previous_state_hash": "click333",
                        "state_changed": False,
                        "failed_request_count": 3,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个产品官网", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("failed network request", str(result.get("error", "")))

    def test_reviewer_with_builder_forces_structured_rejection_on_blank_preview(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_run_smoke": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\",\"scores\":{\"layout\":8,\"color\":8,\"typography\":8,\"animation\":7,\"responsive\":8,\"functionality\":8,\"completeness\":8,\"originality\":8}}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个产品官网", difficulty="standard", subtasks=[builder, reviewer])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>stub</body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch(
                    "orchestrator.validate_preview",
                    new=AsyncMock(return_value={
                        "ok": False,
                        "errors": ["Browser smoke test failed"],
                        "warnings": [],
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "smoke": {
                            "status": "fail",
                            "body_text_len": 0,
                            "render_errors": ["Preview appears blank or near-empty: almost no visible content rendered"],
                            "page_errors": [],
                            "console_errors": [],
                            "render_summary": {"readable_text_count": 0, "heading_count": 0, "interactive_count": 0, "image_count": 0, "canvas_count": 0},
                        },
                    }),
                ):
                    result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("requeue_requested"))
        self.assertIn("\"REJECTED\"", str(result.get("output", "")))
        self.assertIn("blank", str(result.get("output", "")).lower())

    def test_reviewer_with_builder_turns_interaction_gate_failure_into_rejection(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "snap111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "scroll222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "target": "text=Get Started", "state_hash": "scroll222", "previous_state_hash": "scroll222", "state_changed": False},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 3},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个产品官网", difficulty="standard", subtasks=[builder, reviewer])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>stub</body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch(
                    "orchestrator.validate_preview",
                    new=AsyncMock(return_value={
                        "ok": True,
                        "errors": [],
                        "warnings": [],
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "smoke": {"status": "skipped"},
                    }),
                ):
                    result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("requeue_requested"))
        self.assertIn("\"REJECTED\"", str(result.get("output", "")))
        self.assertIn("state", str(result.get("output", "")).lower())


class TestCustomNodeTaskDescriptions(unittest.TestCase):
    def test_scribe_custom_node_description_is_specific(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desc = orch._custom_node_task_desc("scribe", "Scribe", "写一份产品 API 文档")
        self.assertIn("documentation", desc.lower())
        self.assertIn("examples", desc.lower())

    def test_imagegen_custom_node_description_mentions_prompt_packs(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desc = orch._custom_node_task_desc("imagegen", "Image Gen", "生成一张品牌海报")
        self.assertIn("prompt packs", desc.lower())
        self.assertIn("fallback", desc.lower())

    def test_enforce_plan_shape_preserves_specialized_nodes(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="生成品牌海报并输出文档",
            difficulty="standard",
            subtasks=[
                SubTask(id="1", agent_type="imagegen", description="", depends_on=[]),
                SubTask(id="2", agent_type="scribe", description="", depends_on=["1"]),
            ],
        )
        orch._enforce_plan_shape(plan, plan.goal, plan.difficulty)
        self.assertEqual([st.agent_type for st in plan.subtasks], ["imagegen", "scribe"])
        self.assertTrue(all(str(st.description or "").strip() for st in plan.subtasks))

    def test_mixed_specialized_plan_gets_canonicalized(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        }), executor=None, on_event=None)
        plan = Plan(
            goal="做一个像素风平台跳跃游戏，包含角色素材和 spritesheet",
            difficulty="standard",
            subtasks=[
                SubTask(id="1", agent_type="imagegen", description="draw assets", depends_on=[]),
                SubTask(id="2", agent_type="builder", description="build game", depends_on=["1"]),
            ],
        )
        orch._enforce_plan_shape(plan, plan.goal, plan.difficulty)
        self.assertEqual(
            [st.agent_type for st in plan.subtasks],
            ["analyst", "imagegen", "spritesheet", "assetimport", "builder", "reviewer", "deployer", "tester"],
        )


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
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
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

    def test_tester_game_requires_keyboard_press(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "snapshot",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                })
                await on_progress({
                    "stage": "browser_action",
                    "action": "click",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                })
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
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("multiple gameplay key inputs", str(result.get("error", "")))

    def test_tester_dashboard_requires_visible_state_change(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "dash111",
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "target": "text=Monthly",
                        "state_hash": "dash111",
                        "previous_state_hash": "dash111",
                        "state_changed": False,
                    },
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "dash111",
                        "previous_state_hash": "dash111",
                        "state_changed": False,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 3},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个数据看板仪表盘", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("visible state", str(result.get("error", "")))


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
