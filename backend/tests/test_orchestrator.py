import unittest
import asyncio
import tempfile
import time
import os
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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


class TestPolisherFlow(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_custom_polisher_description_preserves_structure(self):
        desc = self.orch._custom_node_task_desc("polisher", "Polisher", "做一个像苹果一样高级的 8 页奢侈品官网")
        self.assertIn("Refine the strongest existing deliverable", desc)
        self.assertIn("Do NOT collapse the site to fewer pages", desc)
        self.assertIn("styles.css and app.js upgrades FIRST", desc)
        self.assertIn("patch at most 2 HTML files", desc)

    def test_reviewer_task_description_allows_direct_route_coverage_after_nav_validation(self):
        desc = self.orch._reviewer_task_description("做一个三页面官网，包含首页、定价页和联系页")
        self.assertIn("first verify at least one real internal navigation path", desc)
        self.assertIn("direct page visits are acceptable", desc)

    def test_deep_mode_complex_website_includes_polisher(self):
        subtasks = self.orch._build_pro_plan_subtasks("做一个像苹果一样高级的 8 页奢侈品官网，要有电影感动画转场")
        agent_types = [st.agent_type for st in subtasks]
        self.assertIn("polisher", agent_types)
        builders = [st for st in subtasks if st.agent_type == "builder"]
        self.assertEqual(len(builders), 2)
        self.assertEqual(builders[0].depends_on, ["1", "2"])
        self.assertEqual(builders[1].depends_on, ["1", "2"])
        polisher = next(st for st in subtasks if st.agent_type == "polisher")
        reviewer = next(st for st in subtasks if st.agent_type == "reviewer")
        self.assertEqual(polisher.depends_on, [builders[0].id, builders[1].id, "3"])
        self.assertEqual(reviewer.depends_on, [polisher.id])


class TestReviewerReworkBrief(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_format_reviewer_rework_brief_groups_builder_and_polisher_actions(self):
        reviewer_output = json.dumps({
            "verdict": "REJECTED",
            "scores": {
                "layout": 6,
                "color": 4,
                "typography": 5,
                "animation": 5,
                "responsive": 7,
                "functionality": 5,
                "completeness": 5,
                "originality": 5,
            },
            "blocking_issues": [
                "index.html uses a flat black background with weak card separation.",
                "cities.html is missing a real visual anchor above the fold.",
            ],
            "required_changes": [
                "Builder: replace the generic hero with topic-matched Beijing / China travel imagery on index.html.",
                "Polisher: tighten typography and nav spacing on cities.html.",
            ],
            "acceptance_criteria": [
                "index.html and cities.html both show layered palette treatment and route-appropriate visuals.",
            ],
            "strengths": [
                "Navigation structure is already coherent across routes.",
            ],
        })

        brief = self.orch._format_reviewer_rework_brief(reviewer_output)

        self.assertIn("Route-specific issues:", brief)
        self.assertIn("Builder fixes:", brief)
        self.assertIn("Polisher follow-up:", brief)
        self.assertIn("index.html", brief)
        self.assertIn("cities.html", brief)
        self.assertIn("extend the palette", brief.lower())


class TestBuilderFailureCleanup(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_cleanup_internal_builder_artifacts_removes_scaffolds_but_keeps_real_page(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            scaffold = out / "index.html"
            scaffold.write_text(
                "<!DOCTYPE html><html><body><!-- evermind-bootstrap scaffold --></body></html>",
                encoding="utf-8",
            )
            partial = out / "_partial_builder.html"
            partial.write_text("<!DOCTYPE html><html><body>partial</body></html>", encoding="utf-8")
            real_page = out / "about.html"
            real_page.write_text("<!DOCTYPE html><html><body>real page</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                removed = self.orch._cleanup_internal_builder_artifacts()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertIn(str(scaffold), removed)
            self.assertIn(str(partial), removed)
            self.assertFalse(scaffold.exists())
            self.assertFalse(partial.exists())
            self.assertTrue(real_page.exists())

    def test_validate_builder_quality_rejects_unfinished_visual_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            about_html = out / "about.html"
            styles_css = out / "styles.css"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>旅行首页</title><link rel="stylesheet" href="styles.css"></head>
<body><nav><a href="index.html">首页</a><a href="about.html">关于</a></nav>
<main><section class="destination-placeholder"></section><section><h1>首页</h1><p>完整内容。</p></section></main>
</body></html>""",
                encoding="utf-8",
            )
            about_html.write_text(
                """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>关于我们</title><link rel="stylesheet" href="styles.css"></head>
<body><nav><a href="index.html">首页</a><a href="about.html">关于</a></nav>
<main><section><h1>关于</h1><p>这是完整的介绍页面。</p></section></main>
</body></html>""",
                encoding="utf-8",
            )
            styles_css.write_text(
                ".destination-placeholder{min-height:280px;background:linear-gradient(135deg,#111,#333);}"
                "nav{display:flex;gap:12px}main{display:grid;gap:24px}section{padding:24px}",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "website"
                report = self.orch._validate_builder_quality(
                    [str(index_html), str(about_html), str(styles_css)],
                    "",
                    goal="做一个两页旅游网站，包含首页和关于页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(
            any("unfinished visual placeholders" in str(item).lower() for item in report.get("errors", []))
        )

    def test_validate_builder_quality_rejects_flat_monochrome_website_palette(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            styles_css = out / "styles.css"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>中国旅行首页</title><link rel="stylesheet" href="styles.css"></head>
<body><header><nav><a href="index.html">首页</a><a href="cities.html">城市</a></nav></header>
<main><section><h1>探索中国</h1><p>这是一个有足够内容密度的高端旅行首页，用于验证纯黑白单色背景会被质量门禁拦截，而不是因为内容太少失败。</p><p>页面依然保留完整段落、结构和真实文案，以确保这里触发的是配色质量问题。</p></section><section><h2>城市精选</h2><p>北京、上海、成都和西安等目的地都需要更丰富的层次化背景与视觉节奏。</p></section></main>
</body></html>""",
                encoding="utf-8",
            )
            styles_css.write_text(
                "body{background:#000;color:#fff;}header,section{background:#111;border:1px solid #fff;}nav{display:flex;gap:12px}"
                "main{display:grid;gap:24px;padding:24px}@media (max-width: 768px){nav{flex-wrap:wrap}}",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "website"
                report = self.orch._validate_builder_quality(
                    [str(index_html), str(styles_css)],
                    "",
                    goal="做一个高端中国旅行网站",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("flat/monochrome" in str(item).lower() for item in report.get("errors", [])))

    def test_builder_retry_regression_reasons_detect_stub_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_root = out / "_stable_previews" / "run_1" / "snapshot"
            stable_root.mkdir(parents=True, exist_ok=True)
            stable_index = stable_root / "index.html"
            stable_index.write_text(
                "<!doctype html><html><body>" + ("premium " * 900) + "</body></html>",
                encoding="utf-8",
            )
            (stable_root / "about.html").write_text(
                "<!doctype html><html><body>" + ("about " * 500) + "</body></html>",
                encoding="utf-8",
            )
            current_index = out / "index.html"
            current_index.write_text("<!doctype html><html><body>stub</body></html>", encoding="utf-8")

            builder = SubTask(id="1", agent_type="builder", description="retry build", depends_on=[])
            builder.retries = 1
            builder.error = "Reviewer rejected (round 1): restore the missing pages"

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._stable_preview_path = stable_index
                reasons = self.orch._builder_retry_regression_reasons(builder, [str(current_index)])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(reasons)
        self.assertTrue(any("collapsed" in reason for reason in reasons))


class TestCanonicalArtifactsAndState(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)
        self.orch._canonical_ctx = {
            "task_id": "task_1",
            "run_id": "run_1",
            "is_custom_plan": False,
            "state_snapshot": {"created_at": 1.0},
        }
        self.orch._subtask_ne_map = {"1": "nodeexec_1", "2": "nodeexec_2"}

    def test_persist_tool_artifacts_saves_browser_capture_and_trace(self):
        with tempfile.TemporaryDirectory() as td:
            capture_path = Path(td) / "capture.png"
            trace_path = Path(td) / "trace.zip"
            capture_path.write_bytes(b"png")
            trace_path.write_bytes(b"zip")

            fake_artifact_store = MagicMock()
            fake_artifact_store.save_artifact.side_effect = [
                {"id": "artifact_capture", "artifact_type": "browser_capture", "path": str(capture_path)},
                {"id": "artifact_trace", "artifact_type": "browser_trace", "path": str(trace_path)},
            ]
            fake_ne_store = MagicMock()

            with patch.object(orchestrator_module, "get_artifact_store", return_value=fake_artifact_store), \
                 patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                persisted = self.orch._persist_tool_artifacts("1", [{
                    "_plugin": "browser",
                    "data": {
                        "action": "record_scroll",
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "browser_mode": "headless",
                        "requested_mode": "headless",
                    },
                    "artifacts": [
                        {"type": "image", "path": str(capture_path)},
                        {"type": "trace", "path": str(trace_path)},
                    ],
                }])

        self.assertEqual(len(persisted), 2)
        artifact_types = [call.args[0]["artifact_type"] for call in fake_artifact_store.save_artifact.call_args_list]
        self.assertEqual(artifact_types, ["browser_capture", "browser_trace"])
        fake_ne_store.update_node_execution.assert_any_call("nodeexec_1", {"artifact_ids": ["artifact_capture"]})
        fake_ne_store.update_node_execution.assert_any_call("nodeexec_1", {"artifact_ids": ["artifact_trace"]})

    def test_persist_tool_artifacts_keeps_browser_use_recording_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            video_path = Path(td) / "play.webm"
            video_path.write_bytes(b"webm")

            fake_artifact_store = MagicMock()
            fake_artifact_store.save_artifact.return_value = {
                "id": "artifact_video",
                "artifact_type": "browser_capture",
                "path": str(video_path),
            }
            fake_ne_store = MagicMock()

            with patch.object(orchestrator_module, "get_artifact_store", return_value=fake_artifact_store), \
                 patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                persisted = self.orch._persist_tool_artifacts("1", [{
                    "_plugin": "browser_use",
                    "data": {
                        "final_url": "http://127.0.0.1:8765/preview/index.html",
                        "recording_path": str(video_path),
                        "action_names": ["click_element", "send_keys"],
                    },
                    "artifacts": [
                        {"type": "video", "path": str(video_path)},
                    ],
                }])

        self.assertEqual(len(persisted), 1)
        saved_payload = fake_artifact_store.save_artifact.call_args.args[0]
        self.assertEqual(saved_payload["metadata"]["recording_path"], str(video_path))
        self.assertEqual(saved_payload["metadata"]["action_names"], ["click_element", "send_keys"])

    def test_reconcile_canonical_context_with_plan_persists_state_snapshot(self):
        self.orch._canonical_ctx["node_executions"] = [
            {
                "id": "nodeexec_1",
                "node_key": "builder",
                "input_summary": "old builder summary",
                "depends_on_keys": ["analyst"],
            },
            {
                "id": "nodeexec_2",
                "node_key": "reviewer",
                "input_summary": "old reviewer summary",
                "depends_on_keys": [],
            },
        ]
        plan = Plan(
            goal="Build premium website",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="Write the multi-page site", depends_on=[]),
                SubTask(id="2", agent_type="reviewer", description="Review the shipped pages", depends_on=["1"]),
            ],
        )
        fake_artifact_store = MagicMock()
        fake_ne_store = MagicMock()

        with patch.object(orchestrator_module, "get_artifact_store", return_value=fake_artifact_store), \
             patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
            drift = self.orch._reconcile_canonical_context_with_plan(plan)

        self.assertTrue(drift)
        fake_ne_store.update_node_execution.assert_any_call(
            "nodeexec_1",
            {"depends_on_keys": [], "input_summary": "Write the multi-page site"},
        )
        fake_ne_store.update_node_execution.assert_any_call(
            "nodeexec_2",
            {"depends_on_keys": ["builder"], "input_summary": "Review the shipped pages"},
        )
        saved_payload = fake_artifact_store.save_artifact.call_args.args[0]
        self.assertEqual(saved_payload["artifact_type"], "state_snapshot")
        self.assertEqual(saved_payload["run_id"], "run_1")
        self.assertEqual(self.orch._canonical_ctx["state_snapshot"]["drift_count"], len(drift))
        self.assertIn("reconciled_at", self.orch._canonical_ctx["state_snapshot"])

    def test_hydrate_stable_preview_from_disk_ignores_other_run_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            other_snapshot = out / "_stable_previews" / "run_older" / "1000_builder_quality_pass_task_4"
            other_snapshot.mkdir(parents=True, exist_ok=True)
            (other_snapshot / "index.html").write_text(
                "<!DOCTYPE html><html><body>older stable preview</body></html>",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._run_started_at = 1774492228.883
                self.orch._stable_preview_path = None
                self.orch._stable_preview_files = []
                self.orch._hydrate_stable_preview_from_disk()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIsNone(self.orch._stable_preview_path)
        self.assertEqual(self.orch._stable_preview_files, [])

    def test_restore_root_index_from_stable_preview_keeps_live_root_when_current_run_has_no_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            root_index = out / "index.html"
            root_index.write_text("<!DOCTYPE html><html><body>stale root</body></html>", encoding="utf-8")
            other_snapshot = out / "_stable_previews" / "run_older" / "1000_builder_quality_pass_task_4"
            other_snapshot.mkdir(parents=True, exist_ok=True)
            (other_snapshot / "index.html").write_text(
                "<!DOCTYPE html><html><body>older stable preview</body></html>",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._stable_preview_path = None
                self.orch._restore_root_index_from_stable_preview()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(root_index.exists())
            self.assertIn("stale root", root_index.read_text(encoding="utf-8"))

    def test_current_preview_hint_uses_current_run_stable_preview_only(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_snapshot = out / "_stable_previews" / "run_current" / "1000_builder_quality_pass_task_4"
            stable_snapshot.mkdir(parents=True, exist_ok=True)
            stable_index = stable_snapshot / "index.html"
            stable_index.write_text("<!DOCTYPE html><html><body>current stable</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._stable_preview_path = stable_index
                hint = self.orch._current_preview_hint("做一个八页奢侈品网站", allow_stable_fallback=True)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("stable_snapshot", hint)
        self.assertIn("_stable_previews/run_current", hint)


class TestReviewerGate(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_reviewer_description_uses_consistent_strict_thresholds(self):
        desc = self.orch._reviewer_task_description("做一个高质量官网", pro=True)
        self.assertIn("ANY single dimension score below 5 = AUTOMATIC REJECT", desc)
        self.assertIn("originality", desc)
        self.assertIn("ship_readiness", desc)
        self.assertIn("missing_deliverables", desc)

    def test_reviewer_description_adds_motion_gate_for_motion_brief(self):
        desc = self.orch._reviewer_task_description("做一个像苹果一样高级的 8 页奢侈品官网，要有高级动画和页面过渡", pro=True)
        self.assertIn("MOTION / TRANSITION GATE", desc)
        self.assertIn("hard-cuts between routes", desc)

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

    def test_parse_reviewer_verdict_returns_unknown_when_verdict_missing(self):
        output = (
            "Reviewed navigation, typography, imagery consistency, and motion. "
            "Several observations are listed, but no final JSON verdict was produced."
        )
        self.assertEqual(self.orch._parse_reviewer_verdict(output), "UNKNOWN")

    def test_forced_reviewer_rejection_penalizes_multi_page_regression(self):
        payload = self.orch._build_reviewer_forced_rejection(
            preview_gate={
                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                "errors": [],
                "warnings": [],
                "smoke": {"status": "pass"},
            },
            multi_page_gate={
                "ok": False,
                "expected_pages": 8,
                "html_files": ["index.html"],
                "errors": ["Multi-page delivery incomplete: found 1/8 HTML pages in the current run."],
                "missing_nav_targets": ["features.html", "contact.html"],
                "unlinked_secondary_pages": [],
            },
        )
        parsed = json.loads(payload)
        self.assertEqual(parsed.get("verdict"), "REJECTED")
        self.assertLessEqual(parsed.get("scores", {}).get("completeness", 10), 1)
        self.assertLessEqual(parsed.get("scores", {}).get("functionality", 10), 2)
        self.assertTrue(any("8 requested HTML pages" in str(item) for item in parsed.get("missing_deliverables", [])))
        self.assertLessEqual(int(parsed.get("ship_readiness", 10) or 10), 4)

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

    def test_forced_rejection_absorbs_visual_regression_guidance(self):
        payload = json.loads(self.orch._build_reviewer_forced_rejection(
            preview_gate={
                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                "smoke": {
                    "status": "pass",
                    "body_text_len": 420,
                    "render_summary": {
                        "readable_text_count": 18,
                        "heading_count": 4,
                        "interactive_count": 6,
                        "image_count": 2,
                        "canvas_count": 0,
                    },
                },
                "visual_regression": {
                    "status": "fail",
                    "summary": "Visual regression gate failed: 2 capture(s) diverged sharply from the last approved baseline.",
                    "issues": [
                        "The current full-page layout is 52% shorter than the last approved baseline; lower sections may be missing or collapsed.",
                    ],
                    "suggestions": [
                        "Restore the missing lower sections and page depth before re-review; compare the full-page content stack against the last approved version.",
                    ],
                },
            }
        ))
        self.assertEqual(payload.get("verdict"), "REJECTED")
        self.assertTrue(any("baseline" in item.lower() for item in payload.get("issues", [])))
        self.assertTrue(any("missing lower sections" in item.lower() for item in payload.get("required_changes", [])))
        self.assertTrue(any("current screenshots stay close" in item.lower() for item in payload.get("acceptance_criteria", [])))

    def test_forced_rejection_flags_large_mid_page_blank_gap(self):
        payload = json.loads(self.orch._build_reviewer_forced_rejection(
            preview_gate={
                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                "smoke": {
                    "status": "fail",
                    "body_text_len": 180,
                    "render_errors": [
                        "Large blank vertical gap detected: content disappears for about 1180px between upper and lower sections",
                    ],
                    "render_summary": {
                        "readable_text_count": 16,
                        "heading_count": 4,
                        "interactive_count": 4,
                        "image_count": 1,
                        "canvas_count": 0,
                        "largest_blank_gap": 1180,
                        "blank_gap_count": 1,
                    },
                },
            }
        ))
        self.assertEqual(payload.get("verdict"), "REJECTED")
        self.assertTrue(any("blank band" in item.lower() for item in payload.get("blocking_issues", [])))
        self.assertTrue(any("missing middle sections" in item.lower() for item in payload.get("required_changes", [])))
        self.assertGreaterEqual(int(payload.get("blank_sections_found", 0) or 0), 1)

    def test_interaction_gate_accepts_record_scroll_after_click_for_website_review(self):
        reason = self.orch._interaction_gate_error(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                {"action": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/"},
                {
                    "action": "record_scroll",
                    "ok": True,
                    "state_changed": True,
                    "at_bottom": True,
                    "at_page_bottom": True,
                    "is_scrollable": True,
                    "url": "http://127.0.0.1:8765/preview/",
                },
            ],
            "做一个高级品牌官网",
        )
        self.assertIsNone(reason)

    def test_interaction_gate_names_missing_artifact_pages_for_multi_page_review(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for name in ("index.html", "pricing.html", "contact.html"):
                (out / name).write_text("<!doctype html><html><body></body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                reason = self.orch._interaction_gate_error(
                    "reviewer",
                    "website",
                    [
                        {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                        {"action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "is_scrollable": False},
                        {"action": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                        {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                    ],
                    "做一个三页面官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
        self.assertIn("contact.html", reason)
        self.assertIn("2/3", reason)


class TestAnalystHandoffFallback(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_missing_handoff_sections_are_synthesized(self):
        plan = Plan(
            goal="创建一个介绍奢侈品的英文网站（8页），页面要简约高级，像苹果一样",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="builder", description="build-a"),
                SubTask(id="3", agent_type="builder", description="build-b"),
                SubTask(id="4", agent_type="reviewer", description="review"),
                SubTask(id="5", agent_type="tester", description="test"),
                SubTask(id="6", agent_type="debugger", description="debug"),
            ],
        )
        raw = "Apple-like premium minimalism with cinematic transitions and editorial product storytelling."
        augmented, synthesized, remaining = self.orch._materialize_analyst_handoff(
            plan,
            raw,
            visited_urls=["https://www.apple.com"],
        )
        self.assertIn("reference_sites", synthesized)
        self.assertIn("builder_1_handoff", synthesized)
        self.assertIn("tester_handoff", synthesized)
        self.assertEqual(remaining, [])
        self.assertIn("https://www.apple.com", self.orch._extract_tagged_section(augmented, "reference_sites"))
        self.assertIn("index.html", self.orch._extract_tagged_section(augmented, "builder_1_handoff"))
        self.assertIn("every requested page", self.orch._extract_tagged_section(augmented, "tester_handoff"))

    def test_single_builder_handoff_stays_end_to_end_for_complex_site(self):
        plan = Plan(
            goal="做一个介绍奢侈品的英文网站（8页），页面要简约高级，像苹果一样，并有页面转场动画。",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="uidesign", description="design", depends_on=["1"]),
                SubTask(id="3", agent_type="scribe", description="content", depends_on=["1"]),
                SubTask(id="4", agent_type="builder", description="build", depends_on=["1", "2", "3"]),
                SubTask(id="5", agent_type="reviewer", description="review", depends_on=["4"]),
            ],
        )
        synthesized = self.orch._synthesized_analyst_handoff_sections(plan, "premium editorial direction")
        self.assertIn("ENTIRE routed experience", synthesized["builder_1_handoff"])
        self.assertIn("single builder", synthesized["builder_2_handoff"])


class TestPlannerContextIsolation(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_history_is_ignored_for_new_unrelated_goal(self):
        summary = self.orch._build_context_summary(
            "做一个介绍奢侈品的全新官网",
            conversation_history=[
                {"role": "user", "content": "之前帮我做一个茶叶品牌网站"},
                {"role": "agent", "content": "用了茶叶、竹林和东方绿色配色。"},
            ],
        )
        self.assertEqual(summary, "")

    def test_history_is_kept_for_explicit_continuation_goal(self):
        summary = self.orch._build_context_summary(
            "继续刚才那个奢侈品网站，基于上次结果优化动画和中间页面",
            conversation_history=[
                {"role": "user", "content": "做一个介绍奢侈品的官网"},
                {"role": "agent", "content": "已生成 8 页版本，风格偏苹果系极简。"},
            ],
        )
        self.assertIn("奢侈品", summary)
        self.assertIn("8 页版本", summary)

    def test_history_is_kept_for_implicit_edit_goal_when_artifact_exists(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "index.html").write_text(
                "<!doctype html><html><body><h1>existing artifact</h1></body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                summary = self.orch._build_context_summary(
                    "把这个奢侈品网站再优化一下，用平衡模式修改动画和排版",
                    conversation_history=[
                        {"role": "user", "content": "做一个介绍奢侈品的官网"},
                        {"role": "agent", "content": "已生成 8 页版本，风格偏苹果系极简。"},
                    ],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
        self.assertIn("奢侈品", summary)
        self.assertIn("8 页版本", summary)

    def test_prepare_output_dir_preserves_artifacts_for_implicit_edit_goal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            artifact = tmp_out / "index.html"
            artifact.write_text("<!doctype html><html><body>keep me</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                self.orch._current_goal = "把这个贪吃蛇游戏再优化一下，用平衡模式修改手感和界面"
                self.orch._current_conversation_history = [
                    {"role": "user", "content": "创建一个贪吃蛇小游戏"},
                    {"role": "agent", "content": "已生成一个可运行的贪吃蛇版本。"},
                ]
                self.orch._prepare_output_dir_for_run()
                self.assertTrue(artifact.exists())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output


class TestBuilderRuntimeArtifactMerge(unittest.TestCase):
    def test_runtime_root_index_is_merged_back_into_builder_files(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "about.html", "contact.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="做一个三页面奢侈品官网",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build index.html, about.html, contact.html",
                    started_at=time.time(),
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = time.time()
                root_index = tmp_out / "index.html"
                root_index.write_text("<!doctype html><html><body>home</body></html>", encoding="utf-8")
                merged = orch._merge_builder_runtime_html_files(
                    plan,
                    plan.subtasks[0],
                    [str(tmp_out / "about.html")],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        normalized = {str(Path(item).resolve()) for item in merged}
        self.assertIn(str(root_index.resolve()), normalized)


class TestBuilderHeartbeatOutput(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_builder_heartbeat_before_first_write_mentions_missing_real_files(self):
        text = self.orch._heartbeat_partial_output("builder", 45, has_file_write=False)
        self.assertIn("尚未检测到真实HTML文件落盘", text)

    def test_builder_heartbeat_after_write_returns_execution_progress(self):
        text = self.orch._heartbeat_partial_output("builder", 45, has_file_write=True)
        self.assertIn("正在编写样式和交互逻辑", text)

    def test_parallel_multi_page_builder_descriptions_do_not_both_claim_index(self):
        primary, secondary = self.orch._parallel_builder_task_descriptions(
            "做一个奢侈品品牌官网，总共8个独立页面，不能做成长滚动单页。"
        )
        self.assertIn("Create index.html", primary)
        self.assertIn("You own /tmp/evermind_output/index.html", primary)
        self.assertIn("Do NOT write /tmp/evermind_output/index.html", secondary)
        self.assertNotIn("Create index.html plus", secondary)


class TestBuilderFirstWriteTimeout(unittest.TestCase):
    """Tests for §P0-FIRST-WRITE: builder early abort if no real file written."""

    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_first_write_timeout_constant_exists(self):
        from orchestrator import BUILDER_FIRST_WRITE_TIMEOUT_SEC
        self.assertGreaterEqual(BUILDER_FIRST_WRITE_TIMEOUT_SEC, 60)
        self.assertLessEqual(BUILDER_FIRST_WRITE_TIMEOUT_SEC, 180)

    def test_builder_heartbeat_before_first_write_warns_at_high_elapsed(self):
        text = self.orch._heartbeat_partial_output("builder", 95, has_file_write=False)
        self.assertIn("未检测到真实HTML文件落盘", text)

    def test_builder_heartbeat_after_write_does_not_warn(self):
        text = self.orch._heartbeat_partial_output("builder", 95, has_file_write=True)
        self.assertNotIn("尚未检测到", text)

    def test_scribe_handoff_condensed_tighter_for_builder(self):
        plan = Plan(
            goal="做一个三页面官网",
            subtasks=[
                SubTask(id="1", agent_type="scribe", description="write content"),
                SubTask(id="2", agent_type="builder", description="build", depends_on=["1"]),
            ],
        )
        # Simulate a large scribe output (44000 chars like the real case)
        scribe_output = "A" * 44000
        prev_results = {"1": {"output": scribe_output}}
        # Build context the same way the orchestrator does
        context_parts = []
        subtask = plan.subtasks[1]
        for dep_id in subtask.depends_on:
            dep_result = prev_results.get(dep_id, {})
            dep_task = next((s for s in plan.subtasks if s.id == dep_id), None)
            if dep_task and dep_result.get("output"):
                dep_output = str(dep_result.get("output", ""))
                if dep_task.agent_type == "scribe" and subtask.agent_type == "builder":
                    condensed = self.orch._condense_handoff_seed(dep_output, limit=600)
                    context_parts.append(
                        f"[Result from scribe #{dep_id}]:\n{condensed}"
                    )
                    continue
                context_parts.append(dep_output[:900])
        context = "\n\n".join(context_parts)
        # Verify condensed output is within 600 chars + header
        self.assertLessEqual(len(context), 700)  # 600 body + ~50 header


class TestBuilderExtractionAndRetryGuards(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_extract_code_files_skips_invalid_truncated_html_block(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    "```html index.html\n<!DOCTYPE html><html><head><style>body{opacity:1}... [TRUNCATED]\n```",
                    subtask_id="9",
                    allow_root_index_copy=True,
                    multi_page_required=True,
                    allowed_html_targets=["index.html", "pricing.html"],
                )
                file_exists = (out_dir / "index.html").exists()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [])
        self.assertFalse(file_exists)

    def test_bootstrap_scaffold_preserves_strong_in_progress_page(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            strong_but_incomplete = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Luxury Tea</title>
<style>
:root{--bg:#0a0a0b;--fg:#f4f1ea;--line:rgba(255,255,255,.12);}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:24px;border-bottom:1px solid var(--line)}
main{display:grid;gap:24px;padding:32px}section{padding:28px;border:1px solid var(--line);border-radius:24px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.cta{display:flex;gap:12px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header><nav><a href="index.html">Home</a><a href="pricing.html">Pricing</a><a href="about.html">About</a></nav></header>
<main>
<section><h1>Luxury Tea</h1><p>Editorial-grade homepage already in progress.</p></section>
<section class="grid"><article><h2>Origin</h2><p>Single-estate sourcing.</p></article><article><h2>Craft</h2><p>Hand-finished harvests.</p></article><article><h2>Ritual</h2><p>Concierge tasting service.</p></article></section>
<section class="cta"><button>Book Tasting</button><button>Explore Collection</button></section>
</main>
</body>
"""
            (out_dir / "index.html").write_text(strong_but_incomplete, encoding="utf-8")
            plan = Plan(
                goal="做一个 8 页高级茶叶官网，要有动画和独立页面",
                subtasks=[SubTask(id="1", agent_type="builder", description="build the full site")],
            )
            subtask = plan.subtasks[0]
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                written = self.orch._ensure_builder_bootstrap_scaffold(plan, subtask)
                index_after = (out_dir / "index.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertNotIn(str(out_dir / "index.html"), written)
        self.assertIn("Luxury Tea", index_after)
        self.assertNotIn("evermind-bootstrap scaffold", index_after.lower())

    def test_extract_code_raw_html_fallback_uses_single_allowed_target(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                expected_path = out_dir / "task_9" / "faq.html"
                files = self.orch._extract_and_save_code(
                    "<!DOCTYPE html><html><body><h1>FAQ</h1></body></html>",
                    subtask_id="9",
                    allow_root_index_copy=False,
                    multi_page_required=True,
                    allowed_html_targets=["faq.html"],
                    allow_multi_page_raw_html_fallback=True,
                )
                faq_exists = expected_path.exists()
                index_exists = (out_dir / "index.html").exists()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [str(expected_path)])
        self.assertTrue(faq_exists)
        self.assertFalse(index_exists)

    def test_extract_code_accepts_absolute_output_paths_for_named_builder_files(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            payload = (
                f"```css {out_dir / 'styles.css'}\nbody{{background:#111;color:#fff;}}\n```\n"
                f"```javascript {out_dir / 'app.js'}\nconsole.log('ok')\n```\n"
                f"```html {out_dir / 'index.html'}\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    payload,
                    subtask_id="4",
                    allow_root_index_copy=True,
                    multi_page_required=True,
                    allowed_html_targets=["index.html", "about.html"],
                    allow_multi_page_raw_html_fallback=True,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn(str(out_dir / "styles.css"), files)
        self.assertIn(str(out_dir / "app.js"), files)
        self.assertIn(str(out_dir / "index.html"), files)
        self.assertFalse((out_dir / "task_4" / "styles.css").exists())
        self.assertFalse((out_dir / "task_4" / "index.js").exists())

    def test_save_extracted_code_block_skips_duplicate_shared_asset_retry_merge(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            primary_css = (
                "body{background:#111;color:#f5f5f5;}"
                ".hero{padding:64px 48px;min-height:100vh;display:grid;place-items:center;}"
            )
            secondary_css = (
                ".pricing-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;}"
                ".pricing-card{border:1px solid rgba(255,255,255,.12);padding:24px;border-radius:24px;}"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files: list[str] = []
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=primary_css,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-4",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=secondary_css,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-5",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=secondary_css,
                    files=files,
                    allow_root_index_copy=True,
                    is_retry=True,
                    merge_owner="builder-5",
                )
                merged = (out_dir / "styles.css").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(merged.count("pricing-grid"), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-4"), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-5"), 1)

    def test_save_extracted_code_block_retry_replaces_owner_section_without_dropping_other_builder_assets(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            initial_primary = ".hero{padding:64px 48px;background:#111;color:#f5f5f5;}"
            updated_primary = ".hero{padding:96px 72px;background:#16171c;color:#f8f2e8;}"
            secondary = ".pricing-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;}"
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files: list[str] = []
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=initial_primary,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-4",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=secondary,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-5",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=updated_primary,
                    files=files,
                    allow_root_index_copy=True,
                    is_retry=True,
                    merge_owner="builder-4",
                )
                merged = (out_dir / "styles.css").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn(updated_primary, merged)
        self.assertIn(secondary, merged)
        self.assertNotIn(initial_primary, merged)
        self.assertEqual(merged.count("Builder Asset Start: builder-4"), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-5"), 1)

    def test_save_extracted_code_block_retry_extracts_owner_payload_from_combined_shared_asset(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            initial_primary = ".hero{padding:64px 48px;background:#111;color:#f5f5f5;}"
            updated_primary = ".hero{padding:96px 72px;background:#162033;color:#f8f2e8;}"
            secondary = ".city-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;}"
            retry_payload = (
                "/* ── Builder Asset Start: builder-4 ── */\n"
                f"{updated_primary}\n"
                "/* ── Builder Asset End: builder-4 ── */\n\n"
                "/* ── Builder Asset Start: builder-5 ── */\n"
                f"{secondary}\n"
                "/* ── Builder Asset End: builder-5 ── */\n"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files: list[str] = []
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=initial_primary,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-4",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=secondary,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-5",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=retry_payload,
                    files=files,
                    allow_root_index_copy=True,
                    is_retry=True,
                    merge_owner="builder-4",
                )
                merged = (out_dir / "styles.css").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn(updated_primary, merged)
        self.assertEqual(merged.count(secondary), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-4"), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-5"), 1)

    def test_extract_code_locked_nav_repair_skips_named_shared_asset_blocks(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            payload = (
                "```css styles.css\nbody{background:#faa;color:#111;}\n```\n"
                "```javascript app.js\nconsole.log('skip-me')\n```\n"
                "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    payload,
                    subtask_id="10",
                    allow_root_index_copy=True,
                    multi_page_required=True,
                    allowed_html_targets=["index.html"],
                    allow_multi_page_raw_html_fallback=True,
                    allow_named_shared_asset_blocks=False,
                )
                index_exists = (out_dir / "index.html").exists()
                styles_exists = (out_dir / "styles.css").exists()
                app_exists = (out_dir / "app.js").exists()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [str(out_dir / "index.html")])
        self.assertTrue(index_exists)
        self.assertFalse(styles_exists)
        self.assertFalse(app_exists)

    def test_extract_code_skips_unnamed_multi_page_shared_assets_and_raw_html_fallback(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            payload = (
                "```css\nbody{background:#111;color:#fff;}\n```\n"
                "```javascript\nconsole.log('should-skip')\n```\n"
                "<!DOCTYPE html><html><body><h1>Ambiguous Multi Page</h1></body></html>"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    payload,
                    subtask_id="7",
                    allow_root_index_copy=True,
                    multi_page_required=True,
                    allowed_html_targets=["index.html", "about.html"],
                    allow_multi_page_raw_html_fallback=True,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [])
        self.assertFalse((out_dir / "task_7" / "styles.css").exists())
        self.assertFalse((out_dir / "task_7" / "index.js").exists())
        self.assertFalse((out_dir / "task_7" / "index.html").exists())

    def test_polisher_visual_gap_report_lists_placeholder_targets(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "index.html").write_text(
                """<!DOCTYPE html><html><body>
                <div class="showcase-image" style="background: linear-gradient(135deg,#111,#333);"></div>
                <div class="collection-card-image" style="background: radial-gradient(circle,#222,#000);"></div>
                <div class="experience-visual"></div>
                </body></html>""",
                encoding="utf-8",
            )
            (out_dir / "contact.html").write_text(
                """<!DOCTYPE html><html><body><div class="map-placeholder"></div></body></html>""",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._run_started_at = 0
                report = self.orch._polisher_visual_gap_report()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("Visual Gap Report", report)
        self.assertIn("index.html", report)
        self.assertIn("contact.html", report)
        self.assertIn("showcase-image", report)
        self.assertIn("map-placeholder", report)

    def test_polisher_visual_gap_report_ignores_filled_media_blocks(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "styles.css").write_text(
                ".story-image { background-image: url('https://example.com/story.jpg'); }\n",
                encoding="utf-8",
            )
            (out_dir / "index.html").write_text(
                """<!DOCTYPE html><html><body>
                <div class="showcase-image"><img src="https://example.com/hero.jpg" alt="hero"></div>
                <div class="story-image"></div>
                <div class="craft-placeholder"><svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg></div>
                </body></html>""",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._run_started_at = 0
                report = self.orch._polisher_visual_gap_report()
                gate_errors = self.orch._polisher_gap_gate_errors()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(report, "")
        self.assertEqual(gate_errors, [])

    def test_polisher_gap_gate_flags_placeholder_copy_blocks(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "index.html").write_text(
                """<!DOCTYPE html><html><body>
                <div class="collection-card-image">[Collection Image]</div>
                <div class="map-placeholder"></div>
                </body></html>""",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._run_started_at = 0
                gate_errors = self.orch._polisher_gap_gate_errors()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(gate_errors)
        self.assertIn("placeholder copy", " ".join(gate_errors).lower())

    def test_polisher_gap_report_flags_icon_shells_and_broken_secondary_routes(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "index.html").write_text(
                """<!DOCTYPE html><html><body>
                <nav><a href="features.html">Features</a><a href="about.html">About</a></nav>
                <main><section><h1>Home</h1><p>Premium home.</p></section></main>
                </body></html>""",
                encoding="utf-8",
            )
            (out_dir / "features.html").write_text(
                """<!DOCTYPE html><html><body>
                <nav><a href="index.html">Home</a><a href="gallery.html">Gallery</a></nav>
                <section class="experience-visual"><div class="experience-icon"><svg viewBox="0 0 64 64"><circle cx="32" cy="20" r="12"/><path d="M32 32v8"/><path d="M24 48h16"/></svg></div></section>
                </body></html>""",
                encoding="utf-8",
            )
            (out_dir / "about.html").write_text(
                """<!DOCTYPE html><html><body>
                <nav><a href="index.html">Home</a></nav>
                <div class="hero-pattern"></div>
                </body></html>""",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._run_started_at = 0
                report = self.orch._polisher_visual_gap_report()
                gate_errors = self.orch._polisher_gap_gate_errors()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("features.html", report)
        self.assertIn("about.html", report)
        self.assertIn("experience-visual", report)
        self.assertIn("hero-pattern", report)
        self.assertIn("broken local routes -> gallery.html", report)
        self.assertTrue(any("gallery.html" in item for item in gate_errors))
        self.assertTrue(any("icon/pattern placeholder visuals" in item for item in gate_errors))

    def test_polisher_regression_guard_restores_stable_output(self):
        class StubBridge:
            config = {}

            def preferred_model_for_node(self, node, model):
                return model

            def _resolve_model(self, model_name):
                return {"provider": "kimi" if "kimi" in str(model_name) else "openai"}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "polished", "tool_results": [], "tool_call_stats": {}}

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        plan = Plan(
            goal="做一个高端多页面旅游网站",
            subtasks=[SubTask(id="5", agent_type="polisher", description="polish the premium site", depends_on=[])],
        )
        subtask = plan.subtasks[0]

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "index.html").write_text(
                "<!DOCTYPE html><html><head><style>body{background:#111;color:#fff}</style></head><body><main><section><h1>Site</h1><p>ok</p></section></main></body></html>",
                encoding="utf-8",
            )
            with patch.object(orchestrator_module, "OUTPUT_DIR", out_dir), \
                 patch.object(
                     orch,
                     "_validate_builder_quality",
                     side_effect=[
                         {"pass": True, "score": 88, "errors": [], "warnings": []},
                         {"pass": True, "score": 70, "errors": [], "warnings": []},
                     ],
                 ), \
                 patch.object(orch, "_polisher_gap_gate_errors", return_value=[]), \
                 patch.object(orch, "_restore_output_from_stable_preview", return_value=[str(out_dir / "index.html")]) as restore_mock:
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("Polisher regression guard failed", str(result.get("error")))
        restore_mock.assert_called_once()


class TestVisualBaselineRefresh(unittest.TestCase):
    def test_successful_run_refreshes_visual_baseline_and_updates_report(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._canonical_ctx = {"task_id": "task_42", "run_id": "run_99"}
        report: dict[str, object] = {}

        with patch.object(orchestrator_module, "latest_preview_artifact", return_value=("task_preview", Path("/tmp/index.html"))), \
             patch.object(orchestrator_module, "build_preview_url_for_file", return_value="http://127.0.0.1:8765/preview/index.html"), \
             patch.object(
                 orchestrator_module,
                 "update_visual_baseline",
                 new=AsyncMock(return_value={
                     "updated": True,
                     "scope_key": "task_task_42",
                     "page_key": "index.html",
                     "captures": [{"name": "desktop_fold", "width": 1440, "height": 1100}],
                 }),
             ) as update_mock:
            asyncio.run(orch._refresh_visual_baseline_for_success("做一个高质量官网", report))

        update_mock.assert_awaited_once()
        call_args = update_mock.await_args
        self.assertEqual(call_args.args[0], "http://127.0.0.1:8765/preview/index.html")
        self.assertEqual(call_args.args[1], "task_task_42")
        self.assertEqual(call_args.kwargs["metadata"]["preview_task_id"], "task_preview")
        self.assertEqual(call_args.kwargs["metadata"]["run_id"], "run_99")
        self.assertEqual(report.get("visual_baseline"), {
            "updated": True,
            "scope_key": "task_task_42",
            "page_key": "index.html",
            "captures": [{"name": "desktop_fold", "width": 1440, "height": 1100}],
        })


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

    def test_report_summary_distinguishes_root_failure_from_blocked_nodes(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="2", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2"]),
                SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["2"]),
            ],
        )
        plan.subtasks[0].status = TaskStatus.FAILED
        plan.subtasks[0].error = "Content completeness failure: 8/19 containers are empty"
        plan.subtasks[1].status = TaskStatus.BLOCKED
        plan.subtasks[1].error = "Blocked by failed dependencies (not executed): 2"
        plan.subtasks[2].status = TaskStatus.BLOCKED
        plan.subtasks[2].error = "Blocked by failed dependencies (not executed): 2"

        report = orch._build_report(plan, results={"2": {"success": False}})

        self.assertFalse(report.get("success"))
        self.assertIn("Root failure", report.get("summary", ""))
        self.assertIn("Downstream blocked", report.get("summary", ""))
        self.assertTrue(any("builder #2" in risk for risk in report.get("remaining_risks", [])))


class TestBuilderQualityGate(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_content_completeness_ignores_game_utility_shells(self):
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Voxel Strike</title>
<style>
body{margin:0;background:#05070d;color:#eef2ff;font-family:sans-serif;overflow:hidden}
main{display:grid;gap:16px;padding:24px}
.hud{display:grid;gap:12px}
.panel{display:grid;gap:8px;padding:16px;border:1px solid rgba(255,255,255,.12)}
.hero{display:grid;gap:12px;min-height:220px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.health-bar{height:8px;background:#142033}
.health-fill{height:100%;width:80%;background:linear-gradient(90deg,#12f7b6,#00c2ff)}
#crosshair,#damageOverlay,#notifications{position:absolute;pointer-events:none}
@media (max-width: 900px){main{padding:16px}.stats{grid-template-columns:1fr}}
</style>
</head>
<body>
<canvas id="gameCanvas"></canvas>
<div id="crosshair"></div>
<div id="damageOverlay"></div>
<div id="notifications"></div>
<main>
  <section class="hero panel">
    <h1>VOXEL STRIKE</h1>
    <p>三维像素射击游戏，包含完整开始界面、战斗 HUD、波次推进、武器切换和移动端触控支持。</p>
    <div class="health-bar"><div class="health-fill"></div></div>
  </section>
  <section class="hud">
    <div class="panel"><h2>任务简报</h2><p>击退敌方波次，收集补给，保持节奏推进并在每轮后升级武器系统。</p></div>
    <div class="stats">
      <article class="panel"><h3>得分</h3><p>12400</p></article>
      <article class="panel"><h3>弹药</h3><p>24 / 180</p></article>
      <article class="panel"><h3>波次</h3><p>第 6 波</p></article>
    </div>
    <div class="panel"><canvas id="minimapCanvas" width="180" height="180"></canvas></div>
  </section>
</main>
<script>window.addEventListener('keydown',()=>{});</script>
</body>
</html>"""
        report = self.orch._html_quality_report(html, source="inline")
        self.assertFalse(any("Content completeness failure" in err for err in report.get("errors", [])))
        self.assertTrue(report.get("pass"))

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

    def test_rejects_style_only_black_screen_artifact(self):
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<body>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Black Screen</title>
  <style>
    body { margin: 0; background: #000; color: #fff; }
    .hud { position: fixed; top: 0; left: 0; }
  </style>
</body>
</html>"""
        report = self.orch._html_quality_report(html, source="inline")
        self.assertFalse(report.get("pass"))
        joined = " | ".join(report.get("errors", []))
        self.assertIn("Body lacks meaningful visible content", joined)

    def test_validate_builder_quality_uses_saved_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "index.html"
            p.write_text("<!DOCTYPE html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body><style>body{margin:0;}div{display:flex;}@media(max-width:800px){div{display:block;}}</style><header></header><main><section></section><section></section><footer></footer></main><script>1+1</script></body></html>", encoding="utf-8")
            report = self.orch._validate_builder_quality([str(p)], output="")
            # Can still fail quality score, but should read artifact and produce a structured report.
            self.assertIn("score", report)
            self.assertIn("errors", report)

    def test_validate_builder_quality_accepts_shared_local_stylesheet_for_multi_page(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(
                ":root{--bg:#0d1117;--fg:#f5f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            (tmp_out / "app.js").write_text(
                "document.querySelectorAll('[data-cta]').forEach((item)=>item.addEventListener('click',()=>item.classList.toggle('is-active')));",
                encoding="utf-8",
            )

            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header><nav><a href="index.html">Home</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header>
  <main>
    <section class="panel"><h1>{title}</h1><p>{body}</p></section>
    <section class="grid"><article class="panel"><h2>Story</h2><p>Editorial density with concrete product language, premium service framing, and stronger information scent for commercial visitors.</p></article><article class="panel"><h2>Service</h2><p>Appointment and concierge detail, client reassurance, and routing cues across the site.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Responsive structure, semantic HTML, and route-aware navigation are already wired across every page so the multi-page preview behaves like a coherent brand site instead of disconnected drafts.</p></section>
  </main>
  <footer><p>Footer copy reinforces the premium journey while the shared stylesheet carries the global design system.</p><button data-cta>Reserve</button></footer>
  <script src="app.js"></script>
</body>
</html>"""
            (tmp_out / "index.html").write_text(page_template.format(title="Home", body="Luxury landing page with coherent navigation, premium content density, and enough descriptive copy to remain substantial even when styling is shared through a local stylesheet asset."), encoding="utf-8")
            (tmp_out / "pricing.html").write_text(page_template.format(title="Pricing", body="Structured offer ladder, private consultation tiers, service framing, and commercial detail that makes the page materially complete instead of a thin placeholder."), encoding="utf-8")
            (tmp_out / "contact.html").write_text(page_template.format(title="Contact", body="Boutique contact flow, appointment intake, concierge follow-up, and location guidance so the final route is rich enough for deterministic quality validation."), encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "pricing.html"), str(tmp_out / "contact.html"), str(tmp_out / "styles.css"), str(tmp_out / "app.js")],
                    output="",
                    goal="做一个三页面轻奢品牌官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(report.get("pass"))
        self.assertFalse(any("style" in err.lower() for err in report.get("errors", [])))

    def test_validate_builder_quality_rejects_unsafe_shared_script_contract(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(
                ":root{--bg:#0d1117;--fg:#f5f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            (tmp_out / "app.js").write_text(
                "const rail = document.querySelector('#exclusiveHeroRail');\n"
                "rail.classList.add('active');\n",
                encoding="utf-8",
            )
            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  {rail}
  <header><nav><a href="index.html">Home</a><a href="contact.html">Contact</a></nav></header>
  <main>
    <section class="panel"><h1>{title}</h1><p>{body}</p></section>
    <section class="grid"><article class="panel"><h2>Craft</h2><p>Real content density, material story, and route continuity across the multi-page site.</p></article><article class="panel"><h2>Service</h2><p>Commercial detail, concierge framing, and responsive structure.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Additional copy ensures each page remains materially complete rather than a stub.</p></section>
  </main>
  <footer><p>Footer continuity.</p></footer>
  <script src="app.js"></script>
</body>
</html>"""
            (tmp_out / "index.html").write_text(
                page_template.format(
                    title="Home",
                    rail='<div id="exclusiveHeroRail"></div>',
                    body="Homepage includes the unique rail element that the unsafe shared script expects.",
                ),
                encoding="utf-8",
            )
            (tmp_out / "contact.html").write_text(
                page_template.format(
                    title="Contact",
                    rail="",
                    body="Contact page omits the unique rail element, so the shared script is unsafe across routes.",
                ),
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "contact.html"), str(tmp_out / "styles.css"), str(tmp_out / "app.js")],
                    output="",
                    goal="做一个双页面轻奢官网，包含首页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Shared local script app.js dereferences selector #exclusiveHeroRail" in err for err in report.get("errors", [])))

    def test_validate_builder_quality_checks_existing_assigned_pages_not_just_latest_retry_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(
                ":root{--bg:#0d1117;--fg:#f5f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                ".hero{display:grid;gap:16px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}}",
                encoding="utf-8",
            )
            (tmp_out / "app.js").write_text(
                "const carousel = document.querySelector('#productCarousel');\n"
                "carousel.classList.add('is-ready');\n",
                encoding="utf-8",
            )
            (tmp_out / "index.html").write_text(
                """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title><link rel="stylesheet" href="styles.css"></head>
<body><div id="productCarousel"></div><header><nav id="nav"><a href="pricing.html">Pricing</a></nav></header>
<main><section class="panel hero"><h1>Home</h1><p>Current retry rewrote the homepage and shared assets, but the secondary route still carries a mismatched DOM contract that should be caught even when only the homepage is listed in files_created.</p><p>The page also includes enough real copy density, semantic structure, and editorial detail to stay well above the minimum deterministic quality thresholds.</p></section><section class="grid"><article class="panel"><h2>Story</h2><p>Enough real content to satisfy the size and structure thresholds for deterministic quality review.</p></article><article class="panel"><h2>Motion</h2><p>Shared transitions and navigation logic must stay coherent across every route in the site.</p></article></section><section class="panel"><h2>Detail</h2><p>Additional copy keeps the page commercially substantial and prevents the test artifact from being mistaken for a tiny scaffold.</p></section></main>
<footer><p>Footer continuity.</p></footer><script src="app.js"></script></body></html>""",
                encoding="utf-8",
            )
            (tmp_out / "pricing.html").write_text(
                """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pricing</title><link rel="stylesheet" href="styles.css"></head>
<body><header><nav id="mainNav"><a href="index.html">Home</a></nav></header>
<main><section class="panel hero"><h1>Pricing</h1><p>This secondary route intentionally omits the product carousel node that the shared script expects, which is exactly the retry-mixing bug the quality gate must catch.</p><p>The pricing route still contains substantial commercial copy so the test failure cannot be attributed to a low-content stub.</p></section><section class="grid"><article class="panel"><h2>Tiers</h2><p>Structured commercial content, service framing, and pricing details make the page materially complete.</p></article><article class="panel"><h2>Support</h2><p>Additional content keeps the route substantial and commercially realistic.</p></article></section><section class="panel"><h2>Detail</h2><p>Additional copy keeps the route substantial instead of stub-like.</p></section></main>
<footer><p>Footer continuity.</p></footer><script src="app.js"></script></body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "styles.css"), str(tmp_out / "app.js")],
                    output="",
                    goal="做一个两页面数码品牌官网，包含首页和价格页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Shared local script app.js dereferences selector #productCarousel" in err for err in report.get("errors", [])))

    def test_validate_builder_quality_auto_normalizes_shared_route_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(
                ":root{--bg:#0d1117;--fg:#f5f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article,ul{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            (tmp_out / "app.js").write_text(
                "const nav = document.getElementById('nav');\n"
                "const navToggle = document.querySelector('.nav-toggle');\n"
                "const navMenu = document.getElementById('navMenu');\n"
                "const overlay = document.querySelector('.page-transition-overlay');\n"
                "if (nav) nav.classList.add('scrolled');\n"
                "if (navToggle && navMenu) navToggle.addEventListener('click', () => navMenu.classList.toggle('active'));\n"
                "if (overlay) overlay.classList.add('active');\n",
                encoding="utf-8",
            )
            (tmp_out / "index.html").write_text(
                """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title><link rel="stylesheet" href="styles.css"></head>
<body><div class="page-transition-overlay"></div><header><nav id="nav"><button class="nav-toggle" id="navToggle">Menu</button><ul id="navMenu" class="nav-links"><li><a class="nav-link" href="about.html">About</a></li></ul></nav></header>
<main><section class="panel"><h1>Home</h1><p>Homepage uses the original shared hook names and contains enough real content to remain above deterministic quality thresholds.</p></section><section class="grid"><article class="panel"><h2>Story</h2><p>Substantial commercial copy, hierarchy, and route continuity.</p></article><article class="panel"><h2>Motion</h2><p>Shared transitions should remain compatible across every route.</p></article></section><section class="panel"><h2>Detail</h2><p>Additional copy keeps the page materially complete.</p></section></main>
<footer><p>Footer continuity.</p></footer><script src="app.js"></script></body></html>""",
                encoding="utf-8",
            )
            (tmp_out / "about.html").write_text(
                """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>About</title><link rel="stylesheet" href="styles.css"></head>
<body><div class="page-transition"></div><header><nav class="main-nav" id="mainNav"><button class="mobile-menu-toggle" id="mobileMenuToggle">Menu</button><ul id="navLinks" class="nav-links"><li><a class="nav-link" href="index.html">Home</a></li></ul></nav></header>
<main><section class="panel"><h1>About</h1><p>Secondary route intentionally uses the alternate hook names that caused runtime breakage before the post-processing compatibility layer was added.</p></section><section class="grid"><article class="panel"><h2>Team</h2><p>Editorial density and product context keep the route substantive.</p></article><article class="panel"><h2>Culture</h2><p>Additional structured copy ensures this is not a stub.</p></article></section><section class="panel"><h2>Detail</h2><p>More content keeps the route materially complete.</p></section></main>
<footer><p>Footer continuity.</p></footer><script src="app.js"></script></body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "styles.css"), str(tmp_out / "app.js")],
                    output="",
                    goal="做一个两页面数码品牌官网，包含首页和关于页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(report.get("pass"))
        self.assertFalse(any("Shared local script" in err for err in report.get("errors", [])))

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

    def test_validate_builder_quality_fails_when_builder_omits_assigned_pages(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>:root{--bg:#0d1117;--fg:#f6f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><a href="platform.html">Platform</a><a href="contact.html">Contact</a><a href="about.html">About</a><a href="faq.html">FAQ</a></nav></header>
<main><section class="panel"><h1>Luxury home</h1><p>Homepage is complete, but builder one still owes its assigned middle pages.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page in ("platform.html", "contact.html", "about.html", "faq.html"):
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{page}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section class="panel"><h1>{page}</h1><p>Secondary page complete.</p></section></main><footer>{page}</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            plan = Plan(
                goal="做一个 8 页轻奢品牌官网，要有首页和其余多页介绍内容",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home cluster"),
                    SubTask(id="2", agent_type="builder", description="build secondary cluster"),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html")],
                    output="",
                    goal=plan.goal,
                    plan=plan,
                    subtask=plan.subtasks[0],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertFalse(report.get("pass"))
            self.assertTrue(any("Builder did not finish its assigned HTML pages" in err for err in report.get("errors", [])))
            self.assertTrue(any("pricing.html" in err for err in report.get("errors", [])))

    def test_multi_page_quality_gate_rejects_thin_stub_like_pages(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>:root{--bg:#0f1220;--fg:#f5f3ee;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:18px 24px;border-bottom:1px solid var(--line)}"
                "main{display:grid;gap:16px;padding:24px}.panel{padding:18px;border:1px solid var(--line);border-radius:18px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}}</style>"
            )
            for name in ("index.html", "pricing.html", "contact.html"):
                (tmp_out / name).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{name}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header>
<main><section class="panel"><h1>{name}</h1><p>Thin page.</p></section></main><footer>{name}</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "pricing.html"), str(tmp_out / "contact.html")],
                    output="",
                    goal="做一个三页面轻奢官网，包含首页、定价页和联系页，并带高级动画",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("too thin / stub-like" in err for err in report.get("errors", [])))

    def test_multi_page_quality_gate_rejects_corrupted_secondary_pages(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            pricing = tmp_out / "pricing.html"
            contact = tmp_out / "contact.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home</title><style>body{margin:0;background:#111;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:18px 24px}main{display:grid;gap:16px;padding:24px}@media(max-width:900px){nav{flex-wrap:wrap}}</style></head>
<body><header><nav><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header><main><section><h1>Home</h1><p>Real home page with full structure and working links.</p></section><section><h2>Story</h2><p>Luxury editorial layout.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            pricing.write_text(
                "<!DOCTYPE html><html><head><style>body{opacity:1}transition:all .6s ease;... [TRUNCATED]",
                encoding="utf-8",
            )
            contact.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Contact</title><style>body{margin:0;background:#111;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:18px 24px}main{display:grid;gap:16px;padding:24px}@media(max-width:900px){nav{flex-wrap:wrap}}</style></head>
<body><header><nav><a href="index.html">Home</a><a href="pricing.html">Pricing</a></nav></header><main><section><h1>Contact</h1><p>Real contact page.</p></section><section><h2>Appointments</h2><p>Book a visit.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index), str(pricing), str(contact)],
                    output="",
                    goal="做一个三页面奢侈品官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("invalid or corrupted HTML pages" in err for err in report.get("errors", [])))

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

    def test_multi_page_quality_gate_rejects_missing_secondary_pages(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Demo</title>
<style>:root{--bg:#0b1020}body{margin:0;background:var(--bg)}main{display:grid}@media(max-width:700px){main{display:block}}</style>
</head>
<body><main><section><h1>Home</h1><p>Only one page exists.</p></section><footer>Footer</footer></main><script>1</script></body>
</html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index)],
                    output="",
                    goal="做一个三页面官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Multi-page delivery incomplete" in err for err in report.get("errors", [])))

    def test_multi_page_quality_gate_passes_when_all_pages_and_nav_exist(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            pricing = tmp_out / "pricing.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#0b1020;--fg:#e9ecf1;--panel:#121a34;--accent:#3dd5f3;--line:rgba(255,255,255,.08);--gap:16px}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#121a34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}header{position:sticky;top:0;background:rgba(11,16,32,.82);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}"
                "nav{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:18px 24px}"
                "nav .links{display:flex;gap:14px}main{display:grid;gap:var(--gap);padding:24px}.hero,.grid,.cta,.contact-grid{display:grid;gap:16px}"
                ".hero{grid-template-columns:1.3fr .7fr;align-items:center}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}"
                ".grid{grid-template-columns:repeat(3,1fr)}.cta{grid-template-columns:repeat(2,minmax(0,1fr))}.contact-grid{grid-template-columns:repeat(2,1fr)}"
                "a{color:var(--fg)}button{padding:12px 18px;border-radius:999px;border:none;background:var(--accent);color:#062033;font-weight:700}"
                "footer{padding:24px;opacity:.8}.eyebrow{text-transform:uppercase;letter-spacing:.18em;font-size:.75rem;opacity:.7}"
                "@media(max-width:900px){.hero,.grid,.cta,.contact-grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></div><button>Book demo</button></nav></header>
<main>
  <section class="hero">
    <article class="panel"><p class="eyebrow">Platform</p><h1>Operate the rollout from one calm command center.</h1><p>Northstar gives operators a premium multi-page web presence with clear product value, trust signals, and a strong conversion path.</p><p>Use the linked pages to compare plans, talk to sales, and inspect implementation notes without collapsing everything into one scroll.</p></article>
    <article class="panel"><h2>Launch snapshot</h2><p>Conversion-focused hero</p><p>Decision-ready pricing narrative</p><p>Human support and onboarding detail</p></article>
  </section>
  <section class="grid">
    <article class="panel"><h3>Faster onboarding</h3><p>Structured rollout steps, guided setup, and clear ownership.</p></article>
    <article class="panel"><h3>Sharper proof</h3><p>Reference customers, evidence blocks, and concise outcomes.</p></article>
    <article class="panel"><h3>Cleaner handoff</h3><p>Design, QA, and delivery stay aligned across every linked page.</p></article>
  </section>
  <section class="cta">
    <article class="panel"><h3>Read pricing</h3><p>Open the pricing page for plans, packaging, and activation support.</p></article>
    <article class="panel"><h3>Contact the team</h3><p>Use the contact page for sales, onboarding, and enterprise security review.</p></article>
  </section>
</main><footer>Northstar launch kit.</footer><script>document.querySelectorAll('a,button').forEach(el=>el.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            pricing.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pricing</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="index.html">Home</a><a href="contact.html">Contact</a></div><button>Talk to sales</button></nav></header>
<main><section class="panel"><h1>Pricing</h1><p>Three plans with rollout guidance, procurement notes, and security support.</p></section><section class="grid"><article class="panel"><h2>Starter</h2><p>Fast setup for lean teams.</p></article><article class="panel"><h2>Growth</h2><p>Automation, review loops, and stronger collaboration.</p></article><article class="panel"><h2>Enterprise</h2><p>Governance, white-glove onboarding, and custom controls.</p></article></section></main><footer>Transparent plans.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            contact.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="index.html">Home</a><a href="pricing.html">Pricing</a></div><button>Email us</button></nav></header>
<main><section class="panel"><h1>Contact</h1><p>Reach onboarding, support, and enterprise architecture review from one place.</p></section><section class="contact-grid"><article class="panel"><h2>Sales</h2><p>Response in one business day.</p></article><article class="panel"><h2>Support</h2><p>Priority coverage and launch-room escalation paths.</p></article></section></main><footer>Human response, no black box.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index), str(pricing), str(contact)],
                    output="",
                    goal="做一个三页面官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))

    def test_multi_page_quality_gate_auto_patches_root_navigation_when_only_nav_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            collections = tmp_out / "collections.html"
            craftsmanship = tmp_out / "craftsmanship.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#0f1222;--fg:#f3f6ff;--panel:#171b31;--line:rgba(255,255,255,.08);--accent:#d6b36f}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0f1222,#171b31);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:18px;padding:18px 24px;border-bottom:1px solid var(--line)}"
                ".links{display:flex;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}"
                "main{display:grid;gap:18px;padding:24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}a{color:var(--fg)}button{padding:10px 16px;border:none;border-radius:999px;background:var(--accent);color:#241b0d;font-weight:700}"
                "@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Maison Aurelia</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="collection.html">Collection</a><a href="atelier.html">Atelier</a><a href="contact.html">Contact</a></div><button>Visit house</button></nav></header>
<main><section class="panel"><h1>Quiet luxury for modern wardrobes.</h1><p>The homepage already has strong visual direction and full copy, but two navigation slugs still point at filenames that do not exist.</p><p>The linked pages are already on disk and should be preserved, not regenerated.</p></section><section class="grid"><article class="panel"><h2>Signature silhouettes</h2><p>Structured tailoring with refined detail.</p></article><article class="panel"><h2>Studio craft</h2><p>Garment construction and finishing narratives.</p></article><article class="panel"><h2>Private appointments</h2><p>Clienteling, fittings, and concierge services.</p></article></section></main><footer>Maison Aurelia.</footer><script>document.querySelectorAll('a,button').forEach(el=>el.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            collections.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Collections</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="craftsmanship.html">Craftsmanship</a><a href="contact.html">Contact</a></div><button>View looks</button></nav></header>
<main><section class="panel"><h1>Collections</h1><p>Seasonal wardrobe systems, hero garments, and styling sequences for the current collection.</p></section><section class="grid"><article class="panel"><h2>Outerwear</h2><p>Cashmere coats and technical wool layers.</p></article><article class="panel"><h2>Knitwear</h2><p>Fine gauge softness and rich texture.</p></article><article class="panel"><h2>Accessories</h2><p>Leather goods and finishing details.</p></article></section></main><footer>Collections archive.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            craftsmanship.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Craftsmanship</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="contact.html">Contact</a></div><button>Book fitting</button></nav></header>
<main><section class="panel"><h1>Craftsmanship</h1><p>Pattern development, atelier finishing, and material sourcing are already documented in this real page.</p></section><section class="grid"><article class="panel"><h2>Pattern room</h2><p>Architectural drafting and fittings.</p></article><article class="panel"><h2>Fabric lab</h2><p>Hand feel, structure, and drape testing.</p></article><article class="panel"><h2>Final finish</h2><p>Pressing, detailing, and inspection.</p></article></section></main><footer>Craft stories.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            contact.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="craftsmanship.html">Craftsmanship</a></div><button>Email concierge</button></nav></header>
<main><section class="panel"><h1>Contact</h1><p>Appointments, showroom visits, and aftercare inquiries route through this completed contact page.</p></section><section class="grid"><article class="panel"><h2>Appointments</h2><p>Private fitting windows.</p></article><article class="panel"><h2>Showroom</h2><p>Press and wholesale visits.</p></article><article class="panel"><h2>Care</h2><p>Alteration and repair service.</p></article></section></main><footer>Client services.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index), str(collections), str(craftsmanship), str(contact)],
                    output="",
                    goal="做一个四页面官网，包含首页、系列、工艺、联系页",
                )
                patched_index = index.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertIn("data-evermind-site-map", patched_index)
        self.assertIn('href="collections.html"', patched_index)
        self.assertIn('href="craftsmanship.html"', patched_index)
        self.assertTrue(any("Auto-patched homepage navigation" in str(w) for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_auto_patch_removes_dead_extra_root_links(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            collections = tmp_out / "collections.html"
            craftsmanship = tmp_out / "craftsmanship.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#0b1020;--fg:#f6f7fb;--panel:#141b31;--line:rgba(255,255,255,.09);--accent:#d6b36f}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#151d34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:18px;padding:18px 24px;border-bottom:1px solid var(--line)}"
                ".links{display:flex;flex-wrap:wrap;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}"
                "main{display:grid;gap:18px;padding:24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}a{color:var(--fg)}button{padding:10px 16px;border:none;border-radius:999px;background:var(--accent);color:#241b0d;font-weight:700}"
                "@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Maison Aurelia</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="collections.html">Collections</a><a href="craftsmanship.html">Craftsmanship</a><a href="contact.html">Contact</a><a href="destinations.html">Destinations</a></div><button>Visit house</button></nav></header>
<main><section class="panel"><h1>Quiet luxury for modern wardrobes.</h1><p>The homepage is otherwise complete and already links to every real page, but one stale dead link should be removed instead of triggering a rebuild.</p></section><section class="grid"><article class="panel"><h2>Signature silhouettes</h2><p>Structured tailoring with refined detail.</p></article><article class="panel"><h2>Studio craft</h2><p>Garment construction and finishing narratives.</p></article><article class="panel"><h2>Private appointments</h2><p>Clienteling, fittings, and concierge services.</p></article></section></main><footer>Maison Aurelia.</footer><script>document.querySelectorAll('a,button').forEach(el=>el.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            collections.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Collections</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="craftsmanship.html">Craftsmanship</a><a href="contact.html">Contact</a></div><button>View looks</button></nav></header>
<main><section class="panel"><h1>Collections</h1><p>Seasonal wardrobe systems, hero garments, and styling sequences for the current collection.</p></section><section class="grid"><article class="panel"><h2>Outerwear</h2><p>Cashmere coats and technical wool layers.</p></article><article class="panel"><h2>Knitwear</h2><p>Fine gauge softness and rich texture.</p></article><article class="panel"><h2>Accessories</h2><p>Leather goods and finishing details.</p></article></section></main><footer>Collections archive.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            craftsmanship.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Craftsmanship</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="contact.html">Contact</a></div><button>Book fitting</button></nav></header>
<main><section class="panel"><h1>Craftsmanship</h1><p>Pattern development, atelier finishing, and material sourcing are already documented in this real page.</p></section><section class="grid"><article class="panel"><h2>Pattern room</h2><p>Architectural drafting and fittings.</p></article><article class="panel"><h2>Fabric lab</h2><p>Hand feel, structure, and drape testing.</p></article><article class="panel"><h2>Final finish</h2><p>Pressing, detailing, and inspection.</p></article></section></main><footer>Craft stories.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            contact.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="craftsmanship.html">Craftsmanship</a></div><button>Email concierge</button></nav></header>
<main><section class="panel"><h1>Contact</h1><p>Appointments, showroom visits, and aftercare inquiries route through this completed contact page.</p></section><section class="grid"><article class="panel"><h2>Appointments</h2><p>Private fitting windows.</p></article><article class="panel"><h2>Showroom</h2><p>Press and wholesale visits.</p></article><article class="panel"><h2>Care</h2><p>Alteration and repair service.</p></article></section></main><footer>Client services.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index), str(collections), str(craftsmanship), str(contact)],
                    output="",
                    goal="做一个四页面官网，包含首页、系列、工艺、联系页",
                )
                patched_index = index.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertNotIn('href="destinations.html"', patched_index)
        self.assertIn('href="collections.html"', patched_index)
        self.assertIn('href="craftsmanship.html"', patched_index)
        self.assertIn('href="contact.html"', patched_index)
        self.assertTrue(any("Auto-patched homepage navigation" in str(w) for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_warns_while_parallel_builder_sibling_pending(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home</title><style>:root{--bg:#0b1020;--fg:#e9ecf1;--panel:#121a34;--line:rgba(255,255,255,.08)}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#121a34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;padding:18px 24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}main{display:grid;gap:16px;padding:24px}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head>
<body><header><nav><strong>Northstar</strong><a href="pricing.html">Pricing</a></nav></header><main><section class="panel"><h1>Home</h1><p>Initial page ready with real launch narrative, conversion framing, and implementation detail.</p><p>The sibling builder still owes the secondary linked pages, but this page already carries the shared visual system and clear navigation intent.</p></section><section class="grid"><article class="panel"><h2>Ops clarity</h2><p>Structured review flow.</p></article><article class="panel"><h2>Faster shipping</h2><p>Hard quality gates.</p></article><article class="panel"><h2>Cleaner handoff</h2><p>Traceable fixes.</p></article></section><footer>Footer</footer></main><script>1</script></body></html>""",
                encoding="utf-8",
            )
            plan = Plan(
                goal="做一个三页面官网，包含首页、定价页和联系页",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary pages"),
                ],
            )
            plan.subtasks[0].status = TaskStatus.COMPLETED
            plan.subtasks[1].status = TaskStatus.PENDING
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index)],
                    output="",
                    goal=plan.goal,
                    plan=plan,
                    subtask=plan.subtasks[0],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertTrue(any("Multi-page delivery is still incomplete" in w for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_preserves_secondary_builder_when_only_home_nav_needs_patch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            collections = tmp_out / "collections.html"
            craftsmanship = tmp_out / "craftsmanship.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#0f1222;--fg:#f3f6ff;--panel:#171b31;--line:rgba(255,255,255,.08);--accent:#d6b36f}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0f1222,#171b31);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:18px;padding:18px 24px;border-bottom:1px solid var(--line)}"
                ".links{display:flex;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}"
                "main{display:grid;gap:18px;padding:24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}a{color:var(--fg)}button{padding:10px 16px;border:none;border-radius:999px;background:var(--accent);color:#241b0d;font-weight:700}"
                "@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Maison Aurelia</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="collection.html">Collection</a><a href="atelier.html">Atelier</a><a href="contact.html">Contact</a></div><button>Visit house</button></nav></header>
<main><section class="panel"><h1>Quiet luxury for modern wardrobes.</h1><p>The homepage already has strong visual direction and full copy, but two navigation slugs still point at filenames that do not exist.</p><p>The linked pages are already on disk and should be preserved, not regenerated.</p></section><section class="grid"><article class="panel"><h2>Signature silhouettes</h2><p>Structured tailoring with refined detail.</p></article><article class="panel"><h2>Studio craft</h2><p>Garment construction and finishing narratives.</p></article><article class="panel"><h2>Private appointments</h2><p>Clienteling, fittings, and concierge services.</p></article></section></main><footer>Maison Aurelia.</footer><script>document.querySelectorAll('a,button').forEach(el=>el.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            collections.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Collections</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="craftsmanship.html">Craftsmanship</a><a href="contact.html">Contact</a></div><button>View looks</button></nav></header>
<main><section class="panel"><h1>Collections</h1><p>Seasonal wardrobe systems, hero garments, and styling sequences for the current collection.</p></section><section class="grid"><article class="panel"><h2>Outerwear</h2><p>Cashmere coats and technical wool layers.</p></article><article class="panel"><h2>Knitwear</h2><p>Fine gauge softness and rich texture.</p></article><article class="panel"><h2>Accessories</h2><p>Leather goods and finishing details.</p></article></section></main><footer>Collections archive.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            craftsmanship.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Craftsmanship</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="contact.html">Contact</a></div><button>Book fitting</button></nav></header>
<main><section class="panel"><h1>Craftsmanship</h1><p>Pattern development, atelier finishing, and material sourcing are already documented in this real page.</p></section><section class="grid"><article class="panel"><h2>Pattern room</h2><p>Architectural drafting and fittings.</p></article><article class="panel"><h2>Fabric lab</h2><p>Hand feel, structure, and drape testing.</p></article><article class="panel"><h2>Final finish</h2><p>Pressing, detailing, and inspection.</p></article></section></main><footer>Craft stories.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            contact.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="craftsmanship.html">Craftsmanship</a></div><button>Email concierge</button></nav></header>
<main><section class="panel"><h1>Contact</h1><p>Appointments, showroom visits, and aftercare inquiries route through this completed contact page.</p></section><section class="grid"><article class="panel"><h2>Appointments</h2><p>Private fitting windows.</p></article><article class="panel"><h2>Showroom</h2><p>Press and wholesale visits.</p></article><article class="panel"><h2>Care</h2><p>Alteration and repair service.</p></article></section></main><footer>Client services.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            plan = Plan(
                goal="做一个四页面官网，包含首页、系列、工艺、联系页",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary pages"),
                ],
            )
            plan.subtasks[0].status = TaskStatus.COMPLETED
            plan.subtasks[1].status = TaskStatus.COMPLETED
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(collections), str(craftsmanship), str(contact)],
                    output="",
                    goal=plan.goal,
                    plan=plan,
                    subtask=plan.subtasks[1],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertTrue(any("Homepage navigation still needs repair by Builder 1" in w for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_rejects_secondary_builder_preview_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            preview_dir = tmp_out / "task_3"
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_html = preview_dir / "index.html"
            preview_html.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Secondary Preview</title><style>body{margin:0;background:#111;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}main{display:grid;gap:16px;padding:24px}section{display:block;padding:20px;border:1px solid rgba(255,255,255,.1);border-radius:18px}</style></head>
<body><main><section><h1>Only a preview fallback</h1><p>This is not a real named secondary page.</p></section><section><h2>Still incomplete</h2><p>No shared navigation or owned route file was saved.</p></section></main></body></html>""",
                encoding="utf-8",
            )
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、材质、传承、门店、联系页",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary pages"),
                ],
            )
            plan.subtasks[0].status = TaskStatus.IN_PROGRESS
            plan.subtasks[1].status = TaskStatus.COMPLETED
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(preview_html)],
                    output="",
                    goal=plan.goal,
                    plan=plan,
                    subtask=plan.subtasks[1],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("real named HTML page" in err or "named pages like" in err for err in report.get("errors", [])))

    def test_multi_page_quality_gate_rejects_partial_index_part_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            part = tmp_out / "index_part1.html"
            part.write_text("<!doctype html><html><body>part</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(part)],
                    output="",
                    goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、定价、故事、门店、联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Partial index_part artifacts" in err for err in report.get("errors", [])))

    def test_aggregate_multi_page_gate_requeues_all_builders_before_reviewer_runs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home</title><style>body{margin:0;background:#111;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}main{display:grid;gap:16px;padding:24px}section{display:block;padding:20px;border:1px solid rgba(255,255,255,.1);border-radius:18px}</style></head>
<body><main><section><h1>Maison Aurelia</h1><p>Only the homepage exists so far.</p><nav><a href="collections.html">Collections</a></nav></section></main><script>1</script></body></html>""",
                encoding="utf-8",
            )
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、定价、故事、门店、联系页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                    SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1", "2"]),
                ],
            )
            for builder in plan.subtasks[:2]:
                builder.status = TaskStatus.COMPLETED
            results = {"1": {"success": True}, "2": {"success": True}}
            completed = {"1", "2"}
            succeeded = {"1", "2"}
            failed = set()
            self.orch.emit = AsyncMock()

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                changed = asyncio.run(
                    self.orch._enforce_multi_page_builder_aggregate_gate(
                        plan,
                        results,
                        completed,
                        succeeded,
                        failed,
                    )
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(changed)
        self.assertEqual(plan.subtasks[0].status, TaskStatus.PENDING)
        self.assertEqual(plan.subtasks[1].status, TaskStatus.PENDING)
        self.assertEqual(plan.subtasks[0].retries, 1)  # §P0-FIX: retries now correctly incremented
        self.assertEqual(plan.subtasks[1].retries, 1)  # §P0-FIX: retries now correctly incremented
        self.assertNotIn("1", completed)
        self.assertNotIn("2", completed)
        self.assertNotIn("1", succeeded)
        self.assertNotIn("2", succeeded)

    def test_aggregate_multi_page_gate_requeues_only_builder_missing_owned_pages(self):
        class StubBridge:
            config = {}

            def _builder_assigned_html_targets(self, input_data):
                text = str(input_data or "")
                if "build home" in text:
                    return ["index.html", "brand.html", "craftsmanship.html", "collections.html"]
                return ["materials.html", "heritage.html", "contact.html", "faq.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        orch.emit = AsyncMock()
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>:root{--bg:#11141c;--fg:#f5f1ea;--panel:#171b25;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:#11141c;color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:14px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px;background:var(--panel)}"
                "@media(max-width:900px){nav{flex-wrap:wrap}}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><a href="brand.html">Brand</a><a href="craftsmanship.html">Craftsmanship</a><a href="collections.html">Collections</a><a href="materials.html">Materials</a></nav></header>
<main><section class="panel"><h1>Maison Aurelia</h1><p>Homepage and first builder pages are already complete.</p></section></main><footer>Home footer.</footer><script>document.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            for page in ("brand.html", "craftsmanship.html", "collections.html"):
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{page}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section class="panel"><h1>{page}</h1><p>Complete content.</p></section></main><footer>{page}</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、材质、传承、联系、FAQ 页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                    SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1", "2"]),
                ],
            )
            for builder in plan.subtasks[:2]:
                builder.status = TaskStatus.COMPLETED
            results = {"1": {"success": True}, "2": {"success": True}}
            completed = {"1", "2"}
            succeeded = {"1", "2"}
            failed = set()

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                changed = asyncio.run(
                    orch._enforce_multi_page_builder_aggregate_gate(
                        plan,
                        results,
                        completed,
                        succeeded,
                        failed,
                    )
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(changed)
        self.assertEqual(plan.subtasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(plan.subtasks[1].status, TaskStatus.PENDING)
        self.assertEqual(plan.subtasks[0].retries, 0)
        self.assertEqual(plan.subtasks[1].retries, 1)  # §P0-FIX: retries now correctly incremented
        self.assertIn("materials.html", plan.subtasks[1].description)
        self.assertNotIn("materials.html", plan.subtasks[0].description)

    def test_aggregate_multi_page_gate_requeues_only_home_builder_for_nav_slug_mismatch(self):
        """§P1-FIX: root_nav_only issues are now auto-patched by the aggregate gate
        instead of re-queuing builders. Verify that the gate succeeds via auto-patch."""
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            collections = tmp_out / "collections.html"
            craftsmanship = tmp_out / "craftsmanship.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#12131a;--fg:#f5f2ec;--panel:#1a1d26;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#12131a,#1a1d26);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:16px;padding:18px 24px}.links{display:flex;gap:14px}"
                "main{display:grid;gap:16px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.02)}"
                ".grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Maison Aurelia</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="collection.html">Collection</a><a href="atelier.html">Atelier</a><a href="contact.html">Contact</a></div></nav></header>
<main><section class="panel"><h1>Quiet luxury, cinematic motion.</h1><p>The homepage exists, but its navigation still references filenames that are not on disk.</p></section><section class="grid"><article class="panel"><h2>Line</h2><p>Structured silhouettes.</p></article><article class="panel"><h2>Craft</h2><p>Atelier finishing.</p></article><article class="panel"><h2>Service</h2><p>Private appointments.</p></article></section></main><footer>Maison Aurelia.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page, title in [
                (collections, "Collections"),
                (craftsmanship, "Craftsmanship"),
                (contact, "Contact"),
            ]:
                page.write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a></div></nav></header>
<main><section class="panel"><h1>{title}</h1><p>This page is already complete and should be preserved during homepage navigation repair.</p></section><section class="grid"><article class="panel"><h2>Module 1</h2><p>Meaningful content.</p></article><article class="panel"><h2>Module 2</h2><p>Meaningful content.</p></article><article class="panel"><h2>Module 3</h2><p>Meaningful content.</p></article></section></main><footer>{title} footer.</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )
            plan = Plan(
                goal="做一个四页面官网，包含首页、系列、工艺、联系页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                    SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1", "2"]),
                ],
            )
            for builder in plan.subtasks[:2]:
                builder.status = TaskStatus.COMPLETED
            results = {"1": {"success": True}, "2": {"success": True}}
            completed = {"1", "2"}
            succeeded = {"1", "2"}
            failed = set()
            self.orch.emit = AsyncMock()

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                changed = asyncio.run(
                    self.orch._enforce_multi_page_builder_aggregate_gate(
                        plan,
                        results,
                        completed,
                        succeeded,
                        failed,
                    )
                )
                patched_index = index.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        # §P1-FIX: auto-patch should have fixed the nav, so no re-queue needed
        self.assertFalse(changed)
        self.assertEqual(plan.subtasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(plan.subtasks[1].status, TaskStatus.COMPLETED)
        self.assertEqual(plan.subtasks[0].retries, 0)
        self.assertEqual(plan.subtasks[1].retries, 0)
        self.assertIn("1", completed)
        self.assertIn("2", completed)
        self.assertIn("1", succeeded)
        self.assertIn("2", succeeded)
        # Verify the auto-patch actually fixed the index.html nav
        self.assertIn("data-evermind-site-map", patched_index)
        self.assertIn('href="collections.html"', patched_index)
        self.assertIn('href="craftsmanship.html"', patched_index)
        self.assertIn('href="contact.html"', patched_index)

    def test_evaluate_multi_page_artifacts_flags_secondary_dead_local_links(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>body{margin:0;background:#111;color:#f5f2ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "main,section,nav,header,footer{display:block}nav{display:flex;gap:12px;padding:20px}section{padding:24px}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><a href="features.html">Features</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header><main><section><h1>Home</h1><p>Complete homepage.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            (tmp_out / "features.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Features</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a><a href="gallery.html">Gallery</a></nav></header><main><section><h1>Features</h1><p>Strong content.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page in ("pricing.html", "contact.html"):
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{page}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section><h1>{page}</h1><p>Complete page.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                gate = self.orch._evaluate_multi_page_artifacts("做一个四页面官网，包含首页、功能页、价格页、联系页")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(gate.get("ok"))
        self.assertEqual(gate.get("repair_scope"), "nav_repair")
        self.assertIn("features.html -> gallery.html", " ".join(gate.get("broken_local_nav_entries", [])))
        self.assertTrue(any("Broken local navigation links detected" in err for err in gate.get("errors", [])))

    def test_evaluate_multi_page_artifacts_flags_incomplete_secondary_route_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>body{margin:0;background:#111;color:#f5f2ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "main,section,nav,header,footer{display:block}nav{display:flex;gap:12px;padding:20px}section{padding:24px}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><a href="features.html">Features</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header><main><section><h1>Home</h1><p>Complete homepage.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            (tmp_out / "features.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Features</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section><h1>Features</h1><p>Strong content.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page in ("pricing.html", "contact.html"):
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{page}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section><h1>{page}</h1><p>Complete page.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                gate = self.orch._evaluate_multi_page_artifacts("做一个四页面官网，包含首页、功能页、价格页、联系页")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(gate.get("ok"))
        self.assertEqual(gate.get("repair_scope"), "nav_repair")
        self.assertTrue(
            any(
                "features.html missing" in entry
                and "pricing.html" in entry
                and "contact.html" in entry
                for entry in gate.get("secondary_missing_nav_entries", [])
            )
        )
        self.assertTrue(any("Shared navigation is incomplete on generated pages" in err for err in gate.get("errors", [])))

    def test_multi_page_quality_gate_auto_patches_secondary_route_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>:root{--bg:#12131a;--fg:#f5f2ec;--panel:#1a1d26;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#12131a,#1a1d26);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:16px;padding:18px 24px}.links{display:flex;gap:14px}"
                "main{display:grid;gap:16px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.02)}"
                ".grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Northstar</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="features.html">Features</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></div></nav></header>
<main><section class="panel"><h1>Home</h1><p>The homepage is already complete and exposes all routes.</p></section><section class="grid"><article class="panel"><h2>Proof</h2><p>Strong launch narrative.</p></article><article class="panel"><h2>Ops</h2><p>Decision-ready content.</p></article><article class="panel"><h2>Sales</h2><p>Clear conversion path.</p></article></section></main><footer>Northstar footer.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page, title in [("features.html", "Features"), ("pricing.html", "Pricing"), ("contact.html", "Contact")]:
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="index.html">Home</a></div></nav></header>
<main><section class="panel"><h1>{title}</h1><p>This route is complete, but its local navigation is too narrow and should be auto-patched instead of triggering a rebuild.</p></section><section class="grid"><article class="panel"><h2>Module 1</h2><p>Meaningful content.</p></article><article class="panel"><h2>Module 2</h2><p>Meaningful content.</p></article><article class="panel"><h2>Module 3</h2><p>Meaningful content.</p></article></section></main><footer>{title} footer.</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "features.html"), str(tmp_out / "pricing.html"), str(tmp_out / "contact.html")],
                    output="",
                    goal="做一个四页面官网，包含首页、功能页、价格页、联系页",
                )
                patched_features = (tmp_out / "features.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertTrue(any("Auto-patched generated navigation" in str(w) for w in report.get("warnings", [])))
        self.assertIn("data-evermind-site-map", patched_features)
        self.assertIn('href="pricing.html"', patched_features)
        self.assertIn('href="contact.html"', patched_features)


class TestDifficultyPlansAndRetryTargets(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_pro_multi_page_website_focus_uses_real_page_ownership(self):
        focus_1, focus_2 = self.orch._pro_builder_focus("做一个八页面官网，包含首页、产品、方案、案例、定价、关于、博客、联系页")
        self.assertIn("MULTI-PAGE website request", focus_1)
        self.assertIn("/tmp/evermind_output/index.html", focus_1)
        self.assertIn("do NOT write /tmp/evermind_output/index_part1.html", focus_1)
        self.assertIn("do NOT write /tmp/evermind_output/index_part2.html", focus_2)
        self.assertIn("remaining 4 page", focus_2)

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

    def test_pro_prompt_targets_seven_subtasks_for_simple_goal(self):
        prompt = self.orch._planner_prompt_for_difficulty("Build landing page", "pro")
        self.assertIn("7-10 subtasks", prompt)
        self.assertIn("7 subtasks", prompt)
        self.assertIn("MUST have 2 builders", prompt)

    def test_pro_prompt_targets_ten_subtasks_for_complex_multi_page_goal(self):
        prompt = self.orch._planner_prompt_for_difficulty(
            "做一个介绍奢侈品的八页面网站，页面要非常高级，像苹果官网一样，还有电影感动画转场。",
            "pro",
        )
        self.assertIn("10 subtasks", prompt)
        self.assertIn("uidesign", prompt)
        self.assertIn("scribe", prompt)
        self.assertIn("polisher", prompt)
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

    def test_standard_voxel_game_enables_3d_asset_pipeline_when_modeling_is_requested(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        }), executor=None)
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        orch._enforce_plan_shape(
            plan,
            "创建一个我的世界风格的像素设计游戏（3d),地图丰富，要有怪物，机制等等，这款游戏要达到商业级水准，建模之类的都要有",
            "standard",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "imagegen", "spritesheet", "assetimport", "builder", "reviewer", "deployer", "tester"],
        )
        self.assertIn("3D", plan.subtasks[1].description)

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

    def test_enforce_plan_shape_pro_complex_goal_prefers_parallel_builders(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(
            plan,
            "做一个介绍奢侈品的八页面网站，页面要像苹果官网一样高级，并带电影感动画转场。",
            "pro",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "uidesign", "scribe", "builder", "builder", "polisher", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[3].depends_on, ["1", "2"])
        self.assertEqual(plan.subtasks[4].depends_on, ["1", "2"])
        self.assertEqual(plan.subtasks[5].depends_on, ["4", "5", "3"])
        self.assertEqual(plan.subtasks[6].depends_on, ["6"])
        self.assertEqual(plan.subtasks[7].depends_on, ["6"])
        self.assertEqual(plan.subtasks[8].depends_on, ["7", "8"])
        self.assertEqual(plan.subtasks[9].depends_on, ["9"])

    def test_enforce_plan_shape_pro_asset_heavy_goal_canonicalizes_to_ten_nodes(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(
            plan,
            "做一个奢侈品 lookbook 网站，8 页，包含 hero 插画、lookbook 视觉素材和高质量 asset pack。",
            "pro",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "imagegen", "spritesheet", "assetimport", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[4].depends_on, ["1", "4"])
        self.assertEqual(plan.subtasks[5].depends_on, ["1", "4"])

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
        self.assertIn("commercial-grade HTML5 game", plan.subtasks[0].description)
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
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[2].depends_on, ["2"])

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

    def test_plan_marks_router_model_as_default_for_node_preferences(self):
        captured = {}

        class StubBridge:
            async def execute(self, node, plugins, input_data, model, on_progress):
                captured["node"] = dict(node)
                return {"success": False, "error": "planner json parse failed"}

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        asyncio.run(orch._plan("Build a premium jewelry website", "gpt-5.4", difficulty="pro"))

        self.assertEqual(captured["node"]["type"], "router")
        self.assertEqual(captured["node"]["model"], "gpt-5.4")
        self.assertTrue(captured["node"].get("model_is_default"))

    def test_retry_attempt_keeps_run_model_marked_as_default_for_node_preferences(self):
        captured = {}

        class StubBridge:
            config = {}

            def preferred_model_for_node(self, node, model):
                captured["preferred_node"] = dict(node)
                return node.get("model", model)

            def _resolve_model(self, model_name):
                return {"provider": "openai" if "gpt" in str(model_name) else "kimi"}

            async def execute(self, node, plugins, input_data, model, on_progress):
                captured["node"] = dict(node)
                return {
                    "success": True,
                    "output": "<reference_sites>\n- https://example.com\n- https://example.org\n</reference_sites>",
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
        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[])
        subtask.retries = 1
        plan = Plan(goal="Build a premium jewelry website", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured["node"]["model"], "gpt-5.4")
        self.assertTrue(captured["node"].get("model_is_default"))

    def test_plan_fallback_pro_complex_goal_enforces_parallel_builder_quality_path(self):
        class StubBridge:
            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": False, "error": "planner json parse failed"}

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        plan = asyncio.run(
            orch._plan(
                "做一个介绍奢侈品的八页面网站，页面要像苹果官网一样高级，并带电影感动画转场。",
                "kimi-coding",
                difficulty="pro",
            )
        )
        self.assertEqual(len(plan.subtasks), 10)
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "uidesign", "scribe", "builder", "builder", "polisher", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[3].depends_on, ["1", "2"])
        self.assertEqual(plan.subtasks[4].depends_on, ["1", "2"])
        self.assertEqual(plan.subtasks[5].depends_on, ["4", "5", "3"])

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
        self.assertEqual(orch._configured_subtask_timeout("builder"), 960)
        self.assertEqual(orch._configured_subtask_timeout("analyst"), 240)
        self.assertEqual(orch._configured_subtask_timeout("reviewer"), 180)
        self.assertEqual(orch._configured_subtask_timeout("tester"), 360)
        self.assertEqual(orch._configured_subtask_timeout("deployer"), 360)

    def test_builder_execution_timeout_boosts_for_large_direct_multifile_batches(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="4", agent_type="builder", description="build premium site", depends_on=[])
        plan = Plan(goal="Build a premium multi-page website", subtasks=[subtask])
        orch._builder_execution_direct_multifile_mode = lambda _plan, _subtask, _model: True  # type: ignore[method-assign]
        orch._builder_bootstrap_targets = lambda _plan, _subtask: [  # type: ignore[method-assign]
            "index.html",
            "pricing.html",
            "features.html",
            "solutions.html",
            "platform.html",
            "contact.html",
            "about.html",
            "faq.html",
            "security.html",
        ]

        timeout_sec = orch._execution_timeout_for_subtask(plan, subtask, "kimi-coding")

        self.assertEqual(timeout_sec, 1440)

    def test_sync_ne_timeout_budget_adds_watchdog_grace_for_builder(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._subtask_ne_map = {"4": "nodeexec_4"}
        fake_ne_store = MagicMock()
        fake_ne_store.get_node_execution.return_value = {
            "id": "nodeexec_4",
            "timeout_seconds": 1020,
        }

        with patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
            orch._sync_ne_timeout_budget("4", 1320)

        fake_ne_store.update_node_execution.assert_called_once_with(
            "nodeexec_4",
            {"timeout_seconds": 1365},
        )

    def test_retry_prompt_for_analyst_keeps_browser_requirement(self):
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
        # P1 FIX: Timeout retries no longer _force_ browser; they allow it optionally
        self.assertIn("prioritize speed over breadth", captured.get("desc", ""))
        self.assertIn("MAY use the browser tool", captured.get("desc", ""))
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

    def test_retry_prompt_for_polisher_timeout_does_not_crash(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            subtask = SubTask(id="11", agent_type="polisher", description="polish", depends_on=[], max_retries=2)
            subtask.status = TaskStatus.FAILED
            subtask.error = "polisher pre-write timeout after 90s: no real file write was produced."
            plan = Plan(goal="Build premium website", subtasks=[subtask])

            # Polisher timeout should safe-fail, NOT retry
            async def should_not_retry(*args, **kwargs):
                raise AssertionError("polisher safe fallback should avoid retry execution on timeout")

            orch._execute_subtask = should_not_retry  # type: ignore[method-assign]

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(ok)
        self.assertEqual(subtask.status, TaskStatus.COMPLETED)
        self.assertEqual(subtask.error, "")
        self.assertIn("稳定版本", subtask.output)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertIn("failed", statuses)

    def test_polisher_non_write_failure_soft_skips_to_stable_preview(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snapshot = out / "_stable_previews" / "run_demo" / "snap_demo"
            snapshot.mkdir(parents=True, exist_ok=True)
            stable_index = snapshot / "index.html"
            stable_index.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")

            subtask = SubTask(id="12", agent_type="polisher", description="polish", depends_on=[], max_retries=2)
            subtask.status = TaskStatus.FAILED
            subtask.error = "polisher loop guard triggered after 4 non-write tool iterations without any file write."
            plan = Plan(goal="Build premium website", subtasks=[subtask])

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                orch._stable_preview_path = stable_index
                orch._stable_preview_stage = "builder_quality_pass"
                orch._stable_preview_files = [str(stable_index)]

                async def should_not_retry(*args, **kwargs):
                    raise AssertionError("polisher safe fallback should avoid retry execution")

                orch._execute_subtask = should_not_retry  # type: ignore[method-assign]
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(ok)
        self.assertEqual(subtask.status, TaskStatus.COMPLETED)
        self.assertEqual(subtask.error, "")
        self.assertIn("稳定版本", subtask.output)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertIn("failed", statuses)

    def test_retry_prompt_for_builder_incomplete_multi_page_forces_direct_multifile_delivery(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html."
            ),
            depends_on=[],
            max_retries=1,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = "Builder quality gate failed (score=44). Errors: ['Multi-page delivery incomplete: found 1/8 valid HTML pages in the current run.']"
        plan = Plan(goal="创建一个 8 页轻奢品牌网站", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                tmp_out = Path(td)
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text("<!DOCTYPE html><html><body>home</body></html>", encoding="utf-8")
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(ok)
        self.assertIn("DIRECT MULTI-FILE DELIVERY ONLY.", captured.get("desc", ""))
        self.assertIn("HTML TARGET OVERRIDE:", captured.get("desc", ""))
        self.assertIn("Do NOT use browser research, file_ops list, or file_ops read on this retry.", captured.get("desc", ""))
        self.assertIn("Output ONLY fenced code blocks", captured.get("desc", ""))

    def test_builder_bootstrap_targets_honor_override_for_single_builder_retry(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "HTML TARGET OVERRIDE: index.html, security.html\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html, security.html."
            ),
            depends_on=[],
        )
        plan = Plan(goal="创建一个 9 页轻奢品牌网站", subtasks=[subtask])

        self.assertEqual(
            orch._builder_bootstrap_targets(plan, subtask),
            ["index.html", "security.html"],
        )

    def test_builder_timeout_retry_targets_only_remaining_pages(self):
        class Bridge:
            config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "pricing.html", "features.html", "about.html"]

        bridge = Bridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html."
            ),
            depends_on=[],
            max_retries=1,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = "builder execution timeout after 966s."
        plan = Plan(goal="做一个四页面轻奢品牌网站，包含首页、定价页、功能页和关于页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                tmp_out = Path(td)
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><nav><a href="pricing.html">Pricing</a><a href="features.html">Features</a><a href="about.html">About</a></nav><main><section><h1>Home</h1><p>Premium home page.</p></section></main></body></html>""",
                    encoding="utf-8",
                )
                (tmp_out / "pricing.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><main><h1>Pricing</h1><p>Pricing details.</p></main></body></html>""",
                    encoding="utf-8",
                )
                (tmp_out / "features.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><main><h1>Features</h1><p>Feature details.</p></main></body></html>""",
                    encoding="utf-8",
                )
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(ok)
        self.assertIn("DIRECT MULTI-FILE DELIVERY ONLY.", captured.get("desc", ""))
        override_line = next(
            line for line in captured.get("desc", "").splitlines()
            if "HTML TARGET OVERRIDE:" in line
        )
        self.assertIn("index.html", override_line)
        self.assertIn("about.html", override_line)
        self.assertNotIn("pricing.html", override_line)
        self.assertNotIn("features.html", override_line)

    def test_builder_timeout_retry_from_gpt_still_carries_override_targets(self):
        class Bridge:
            config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "pricing.html", "features.html", "about.html"]

        bridge = Bridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html."
            ),
            depends_on=[],
            max_retries=1,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = "builder execution timeout after 966s."
        plan = Plan(goal="做一个四页面轻奢品牌网站，包含首页、定价页、功能页和关于页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                tmp_out = Path(td)
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><nav><a href="pricing.html">Pricing</a><a href="features.html">Features</a><a href="about.html">About</a></nav><main><section><h1>Home</h1><p>Premium home page.</p></section></main></body></html>""",
                    encoding="utf-8",
                )
                (tmp_out / "pricing.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><main><h1>Pricing</h1><p>Pricing details.</p></main></body></html>""",
                    encoding="utf-8",
                )
                (tmp_out / "features.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><main><h1>Features</h1><p>Feature details.</p></main></body></html>""",
                    encoding="utf-8",
                )
                ok = asyncio.run(orch._handle_failure(subtask, plan, "gpt-5.4", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(ok)
        self.assertIn("DIRECT MULTI-FILE DELIVERY ONLY.", captured.get("desc", ""))
        override_line = next(
            line for line in captured.get("desc", "").splitlines()
            if "HTML TARGET OVERRIDE:" in line
        )
        self.assertIn("index.html", override_line)
        self.assertIn("about.html", override_line)
        self.assertNotIn("pricing.html", override_line)
        self.assertNotIn("features.html", override_line)


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


class TestBuilderDirectMultifileRetry(unittest.TestCase):
    def test_builder_auto_direct_text_mode_for_single_page_game(self):
        captured = {}

        class DirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                captured["input_data"] = kwargs.get("input_data") or ""
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n"
                        "<!DOCTYPE html><html><body><main><h1>Voxel Strike</h1><canvas id='game'></canvas></main></body></html>\n"
                        "```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=DirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(goal="创建一个我的世界风格的 3D 像素射击游戏，单页即可", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured.get("node", {}).get("builder_delivery_mode"), "direct_text")
        self.assertIn("DIRECT SINGLE-FILE DELIVERY mode", captured.get("input_data", ""))
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())

    def test_builder_auto_direct_multifile_first_attempt_for_kimi_multi_page(self):
        captured = {}

        class SlowDirectBridge:
            config = {}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                captured["input_data"] = kwargs.get("input_data") or ""
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html pricing.html\n<!DOCTYPE html><html><body><h1>Pricing</h1></body></html>\n```\n"
                        "```html features.html\n<!DOCTYPE html><html><body><h1>Features</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=SlowDirectBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium site",
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面轻奢品牌网站，包含首页、定价页和功能页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured.get("node", {}).get("builder_delivery_mode"), "direct_multifile")
        self.assertIn("DIRECT MULTI-FILE DELIVERY mode", captured.get("input_data", ""))
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())

    def test_builder_direct_multifile_retry_waits_for_text_output_without_first_write(self):
        captured = {}

        class SlowDirectBridge:
            config = {}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html pricing.html\n<!DOCTYPE html><html><body><h1>Pricing</h1></body></html>\n```\n"
                        "```html contact.html\n<!DOCTYPE html><html><body><h1>Contact</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=SlowDirectBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "⚠️ MULTI-PAGE DELIVERY INCOMPLETE.\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "Return index.html, pricing.html, and contact.html."
            ),
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面轻奢品牌网站，包含首页、定价页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured.get("node", {}).get("builder_delivery_mode"), "direct_multifile")
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())

    def test_builder_runtime_fallback_to_kimi_skips_first_write_timeout_and_updates_assignment(self):
        captured = {}

        class FallbackBridge:
            config = {
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            }

            def preferred_model_for_node(self, node, model):
                return "gpt-5.4"

            def _resolve_model(self, model_name):
                provider = "kimi" if model_name == "kimi-coding" else "openai"
                return {"provider": provider}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                on_progress = kwargs.get("on_progress")
                await on_progress({
                    "stage": "model_chain_resolved",
                    "assignedModel": "gpt-5.4",
                    "assignedProvider": "openai",
                    "candidateModels": ["gpt-5.4", "kimi-coding"],
                })
                await on_progress({
                    "stage": "model_selected",
                    "assignedModel": "gpt-5.4",
                    "assignedProvider": "openai",
                    "modelIndex": 1,
                    "modelCount": 2,
                })
                await on_progress({
                    "stage": "model_fallback",
                    "assignedModel": "kimi-coding",
                    "assignedProvider": "kimi",
                    "from_model": "gpt-5.4",
                    "to_model": "kimi-coding",
                })
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html pricing.html\n<!DOCTYPE html><html><body><h1>Pricing</h1></body></html>\n```\n"
                        "```html contact.html\n<!DOCTYPE html><html><body><h1>Contact</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                    "assigned_model": "kimi-coding",
                    "assigned_provider": "kimi",
                }

        orch = Orchestrator(ai_bridge=FallbackBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium multi-page site",
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面轻奢品牌网站，包含首页、定价页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())
        self.assertEqual(captured.get("node", {}).get("model"), "gpt-5.4")
        self.assertTrue(any(
            call.kwargs.get("assigned_model") == "kimi-coding"
            and call.kwargs.get("assigned_provider") == "kimi"
            for call in orch._sync_ne_status.await_args_list
        ))

    def test_builder_runtime_direct_multifile_switch_extends_timeout_budget(self):
        class FallbackBridge:
            config = {
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            }

            def preferred_model_for_node(self, node, model):
                return "gpt-5.4"

            def _resolve_model(self, model_name):
                provider = "kimi" if model_name == "kimi-coding" else "openai"
                return {"provider": provider}

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                await on_progress({
                    "stage": "model_fallback",
                    "assignedModel": "kimi-coding",
                    "assignedProvider": "kimi",
                })
                await asyncio.sleep(1.4)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html pricing.html\n<!DOCTYPE html><html><body><h1>Pricing</h1></body></html>\n```\n"
                        "```html contact.html\n<!DOCTYPE html><html><body><h1>Contact</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=FallbackBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._sync_ne_timeout_budget = Mock()
        orch._configured_progress_heartbeat = lambda: 0.2  # type: ignore[method-assign]
        orch._execution_timeout_for_subtask = lambda plan, subtask, model: 3 if model == "kimi-coding" else 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium multi-page site",
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面轻奢品牌网站，包含首页、定价页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        timeout_calls = [call.args[1] for call in orch._sync_ne_timeout_budget.call_args_list]
        self.assertIn(1, timeout_calls)
        self.assertIn(3, timeout_calls)

    def test_builder_direct_multifile_batch_ready_saves_files_and_skips_idle_timeout(self):
        events = []

        class BatchBridge:
            config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "about.html"]

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                await on_progress({
                    "stage": "builder_multifile_batch_ready",
                    "batch_index": 1,
                    "returned_targets": ["index.html"],
                    "finish_reason": "length",
                    "content": "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```",
                })
                await asyncio.sleep(1.2)
                await on_progress({
                    "stage": "builder_multifile_batch_ready",
                    "batch_index": 2,
                    "returned_targets": ["about.html"],
                    "finish_reason": "stop",
                    "content": "```html about.html\n<!DOCTYPE html><html><body><h1>About</h1></body></html>\n```",
                })
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html about.html\n<!DOCTYPE html><html><body><h1>About</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        async def record(event_type, payload):
            evt = dict(payload or {})
            evt["type"] = event_type
            events.append(evt)

        orch = Orchestrator(ai_bridge=BatchBridge(), executor=None)
        orch.emit = AsyncMock(side_effect=record)
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "⚠️ MULTI-PAGE DELIVERY INCOMPLETE.\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "Assigned HTML filenames for this builder: index.html, about.html.\n"
                "Return index.html and about.html."
            ),
            depends_on=[],
        )
        plan = Plan(goal="做一个两页面轻奢品牌网站，包含首页和关于页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

            index_html = (tmp_out / "index.html").read_text(encoding="utf-8")
            about_html = (tmp_out / "about.html").read_text(encoding="utf-8")

        self.assertTrue(result.get("success"))
        self.assertNotIn("post-write idle timeout", str(result.get("error", "")).lower())
        self.assertIn("<h1>Home</h1>", index_html)
        self.assertIn("<h1>About</h1>", about_html)

        batch_events = [
            evt for evt in events
            if evt.get("type") == "subtask_progress" and evt.get("stage") == "builder_multifile_batch_ready"
        ]
        self.assertEqual(len(batch_events), 2)
        self.assertTrue(all("content" not in evt for evt in batch_events))
        self.assertTrue(any(any(str(path).endswith("index.html") for path in evt.get("saved_files", [])) for evt in batch_events))
        self.assertTrue(any(any(str(path).endswith("about.html") for path in evt.get("saved_files", [])) for evt in batch_events))

    def test_builder_direct_multifile_batch_ready_skips_unassigned_html_targets(self):
        class BatchBridge:
            config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "about.html"]

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                await on_progress({
                    "stage": "builder_multifile_batch_ready",
                    "batch_index": 1,
                    "returned_targets": ["index.html", "destinations.html"],
                    "finish_reason": "length",
                    "content": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html destinations.html\n<!DOCTYPE html><html><body><h1>Destinations</h1></body></html>\n```"
                    ),
                })
                await on_progress({
                    "stage": "builder_multifile_batch_ready",
                    "batch_index": 2,
                    "returned_targets": ["about.html"],
                    "finish_reason": "stop",
                    "content": "```html about.html\n<!DOCTYPE html><html><body><h1>About</h1></body></html>\n```",
                })
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html destinations.html\n<!DOCTYPE html><html><body><h1>Destinations</h1></body></html>\n```\n"
                        "```html about.html\n<!DOCTYPE html><html><body><h1>About</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=BatchBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 0.2  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "Assigned HTML filenames for this builder: index.html, about.html."
            ),
            depends_on=[],
        )
        plan = Plan(goal="做一个两页面轻奢品牌网站，包含首页和关于页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

            index_html = (tmp_out / "index.html").read_text(encoding="utf-8")
            about_html = (tmp_out / "about.html").read_text(encoding="utf-8")

        self.assertTrue(result.get("success"))
        self.assertIn("<h1>Home</h1>", index_html)
        self.assertIn("<h1>About</h1>", about_html)
        self.assertFalse((tmp_out / "destinations.html").exists())

    def test_builder_repo_context_does_not_leave_direct_multifile_flag_unbound(self):
        captured = {}

        class StubBridge:
            config = {}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                return {
                    "success": True,
                    "output": "```html index.html\n<!DOCTYPE html><html><body><h1>Repo</h1></body></html>\n```",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="patch existing repo page", depends_on=[])
        plan = Plan(goal="修复现有仓库里的官网页面", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            repo_context = {"repo_root": "/tmp/demo-repo", "verification_commands": ["npm test"], "activity_note": ""}
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "build_repo_context", return_value=repo_context), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("builder_delivery_mode", captured.get("node", {}))

    def test_nav_only_execute_subtask_restores_locked_root_artifacts(self):
        original_styles = "body{background:#101010;color:#f5f5f5;}"
        original_about = "<!DOCTYPE html><html><body><h1>About Stable</h1></body></html>"

        class NavRepairBridge:
            config = {}

            async def execute(self, **kwargs):
                out_dir = orchestrator_module.OUTPUT_DIR
                (out_dir / "styles.css").write_text("body{background:#ff66aa;color:#111;}", encoding="utf-8")
                (out_dir / "about.html").write_text(
                    "<!DOCTYPE html><html><body><h1>About Broken</h1></body></html>",
                    encoding="utf-8",
                )
                (out_dir / "faq.html").write_text(
                    "<!DOCTYPE html><html><body><h1>Should Not Exist</h1></body></html>",
                    encoding="utf-8",
                )
                return {
                    "success": True,
                    "output": (
                        "I'll first inspect the existing files.\n"
                        "```css styles.css\nbody{background:#ff66aa;color:#111;}\n```\n"
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home Fixed</h1><nav><a href=\"about.html\">About</a></nav></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=NavRepairBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._sync_ne_timeout_budget = Mock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "⚠️ NAVIGATION REPAIR ONLY.\n"
                f"{orchestrator_module.BUILDER_NAV_REPAIR_ONLY_MARKER}\n"
                f"{orchestrator_module.BUILDER_TARGET_OVERRIDE_MARKER} index.html\n"
                "Assigned HTML filenames for this builder: index.html, about.html, contact.html.\n"
            ),
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面官网，包含首页、关于页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(original_styles, encoding="utf-8")
            (tmp_out / "about.html").write_text(original_about, encoding="utf-8")
            (tmp_out / "contact.html").write_text(
                "<!DOCTYPE html><html><body><h1>Contact Stable</h1></body></html>",
                encoding="utf-8",
            )
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

            restored_styles = (tmp_out / "styles.css").read_text(encoding="utf-8")
            restored_about = (tmp_out / "about.html").read_text(encoding="utf-8")
            repaired_index = (tmp_out / "index.html").read_text(encoding="utf-8")

        self.assertTrue(result.get("success"))
        self.assertEqual(restored_styles, original_styles)
        self.assertEqual(restored_about, original_about)
        self.assertFalse((tmp_out / "faq.html").exists())
        self.assertIn("<h1>Home Fixed</h1>", repaired_index)
        self.assertTrue(all(Path(path).name == "index.html" for path in result.get("files_created", [])))
        self.assertTrue(any(
            call.args[0] == "subtask_progress"
            and call.args[1].get("stage") == "builder_nav_repair_locked_restore"
            for call in orch.emit.await_args_list
        ))


class TestBuilderPreviewExposure(unittest.TestCase):
    def test_builder_quality_failure_does_not_emit_preview_ready(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            html = tmp_out / "index.html"
            html.write_text(
                "<!doctype html><html><head><title>Demo</title></head><body><main>draft</main></body></html>",
                encoding="utf-8",
            )

            class StubBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "builder finished",
                        "tool_results": [{"written": True, "path": str(html)}],
                        "tool_call_stats": {},
                    }

            events = []

            async def record(event_type, payload):
                evt = dict(payload or {})
                evt["type"] = event_type
                events.append(evt)

            orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
            orch.emit = AsyncMock(side_effect=record)
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            plan = Plan(goal="Build premium website", subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[])])
            subtask = plan.subtasks[0]

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 91}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": False, "score": 38, "errors": ["Missing middle content"], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertFalse(any(evt.get("type") == "preview_ready" for evt in events))
        self.assertTrue(any(evt.get("stage") == "quality_gate_failed" for evt in events))

    def test_builder_emits_preview_only_after_quality_gate_passes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            html = tmp_out / "index.html"
            html.write_text(
                "<!doctype html><html><head><title>Demo</title></head><body><main>final</main></body></html>",
                encoding="utf-8",
            )

            class StubBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "builder finished",
                        "tool_results": [{"written": True, "path": str(html)}],
                        "tool_call_stats": {},
                    }

            events = []

            async def record(event_type, payload):
                evt = dict(payload or {})
                evt["type"] = event_type
                events.append(evt)

            orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
            orch.emit = AsyncMock(side_effect=record)
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            plan = Plan(goal="Build premium website", subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[])])
            subtask = plan.subtasks[0]

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 94}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        preview_indexes = [idx for idx, evt in enumerate(events) if evt.get("type") == "preview_ready"]
        self.assertEqual(len(preview_indexes), 1)
        quality_indexes = [
            idx for idx, evt in enumerate(events)
            if evt.get("type") == "subtask_progress" and evt.get("stage") == "quality_gate"
        ]
        self.assertTrue(quality_indexes)
        self.assertGreater(preview_indexes[0], quality_indexes[-1])
        self.assertFalse(events[preview_indexes[0]].get("final", True))


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

    async def test_sync_ne_status_can_reset_started_at_for_retry(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._canonical_ctx = {"task_id": "task_1", "run_id": "run_1"}
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        fake_ne_store = MagicMock()
        fake_ne_store.get_node_execution.return_value = {
            "id": "nodeexec_1",
            "node_key": "builder",
            "node_label": "builder",
            "status": "running",
            "assigned_model": "kimi-coding",
            "assigned_provider": "",
            "retry_count": 0,
            "tokens_used": 0,
            "cost": 0.0,
            "input_summary": "builder task",
            "output_summary": "",
            "error_message": "",
            "artifact_ids": [],
            "started_at": 1.0,
            "ended_at": 0.0,
            "created_at": 1.0,
            "progress": 5,
            "phase": "running",
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

        before = time.time()
        with patch.dict(sys.modules, {"__main__": fake_main}, clear=False):
            with patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                with patch.object(orchestrator_module, "get_run_store", return_value=fake_run_store):
                    await orch._sync_ne_status(
                        "1",
                        "running",
                        phase="retrying",
                        retry_count=2,
                        reset_started_at=True,
                    )

        update_payload = fake_ne_store.update_node_execution.call_args.args[1]
        self.assertEqual(update_payload["retry_count"], 2)
        self.assertEqual(update_payload["ended_at"], 0.0)
        self.assertGreaterEqual(update_payload["started_at"], before)

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

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))
                root_index_exists = (Path(td) / "index.html").exists()
                root_index = (Path(td) / "index.html").read_text(encoding="utf-8") if root_index_exists else ""
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(result.get("success"))
        self.assertIn("<!DOCTYPE html>", str(result.get("output", "")))
        self.assertIn("partial-content", subtask.last_partial_output)
        self.assertTrue(root_index_exists)
        self.assertIn("partial-content", root_index)


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

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(ok)
        self.assertIn("上次执行因超时中断", captured.get("desc", ""))
        self.assertIn("_partial_builder.html", captured.get("desc", ""))
        self.assertIn(td, captured.get("desc", ""))
        self.assertIn("不要从零开始重写", captured.get("desc", ""))
        self.assertNotIn("MAX 150 lines", captured.get("desc", ""))
        self.assertNotIn("MAX 100 lines", captured.get("desc", ""))
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

    def test_extract_and_save_code_can_skip_root_copy_for_secondary_builder(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Secondary</title></head>
<body><main>secondary page</main></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "secondary", allow_root_index_copy=False)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any(str(p).endswith("/task_secondary/index.html") for p in files))
        self.assertFalse(any(Path(p).parent == Path(td) for p in files))

    def test_extract_and_save_code_supports_named_multi_file_blocks(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html index.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Home</title></head>
<body><a href="collections.html">Collections</a></body>
</html>
```
```html collections.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collections</title></head>
<body><a href="index.html">Home</a></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "multi")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            home = (Path(td) / "index.html").read_text(encoding="utf-8")
            collections = (Path(td) / "collections.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "index.html"), files)
        self.assertIn(str(Path(td) / "collections.html"), files)
        self.assertIn("Collections", home)
        self.assertIn("Home", collections)

    def test_extract_and_save_code_recovers_unterminated_named_multi_file_block(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html index.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Home</title></head>
<body><a href="collections.html">Collections</a></body>
</html>
```
```html collections.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collections</title></head>
<body><a href="index.html">Home</a></body>
</html>"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "unterminated", multi_page_required=True)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            home = (Path(td) / "index.html").read_text(encoding="utf-8")
            collections = (Path(td) / "collections.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "index.html"), files)
        self.assertIn(str(Path(td) / "collections.html"), files)
        self.assertIn("Collections", home)
        self.assertIn("Home", collections)

    def test_extract_and_save_code_recovers_lossy_multi_file_headers(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html index.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Home</title></head>
<body><a href="collections.html">Collections</a></body>
</html>
```html collections.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collections</title></head>
<body><a href="index.html">Home</a></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "lossy_headers", multi_page_required=True)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            home = (Path(td) / "index.html").read_text(encoding="utf-8")
            collections = (Path(td) / "collections.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "index.html"), files)
        self.assertIn(str(Path(td) / "collections.html"), files)
        self.assertIn("Collections", home)
        self.assertIn("Home", collections)

    def test_extract_and_save_code_skips_single_unnamed_html_for_multi_page_requests(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """The blocked site failed to load.
```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collapsed</title></head>
<body><main>only one unnamed page</main></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "multi_skip", multi_page_required=True)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [])

    def test_extract_and_save_code_can_salvage_single_raw_html_for_multi_page_timeout(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Home</title></head>
<body><main><section>Recovered partial homepage</section></main></body>
</html>"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(
                    output,
                    "multi_timeout_salvage",
                    multi_page_required=True,
                    allow_multi_page_raw_html_fallback=True,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            saved = (Path(td) / "index.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "index.html"), files)
        self.assertIn("Recovered partial homepage", saved)

    def test_extract_and_save_code_remaps_skipped_named_blocks_when_count_matches_assignment(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html collections.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collections</title></head>
<body><main><section>First recovered page</section></main></body>
</html>
```
```html heritage.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Heritage</title></head>
<body><main><section>Second recovered page</section></main></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(
                    output,
                    "remap_targets",
                    multi_page_required=True,
                    allowed_html_targets=["pricing.html", "features.html"],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            pricing = (Path(td) / "pricing.html").read_text(encoding="utf-8")
            features = (Path(td) / "features.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "pricing.html"), files)
        self.assertIn(str(Path(td) / "features.html"), files)
        self.assertIn("First recovered page", pricing)
        self.assertIn("Second recovered page", features)

    def test_non_root_builder_root_overwrite_is_rejected_and_restored(self):
        class StubBridge:
            def __init__(self, root_path: Path):
                self.config = {}
                self.root_path = root_path

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "saved",
                    "tool_results": [{"written": True, "path": str(self.root_path)}],
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            stable_html = tmp_out / "_stable_previews" / "run_test" / "approved_task_1" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            root_html.write_text(
                "<!doctype html><html><head><title>Good</title></head><body><main><section>good root</section></main><script>1</script></body></html>",
                encoding="utf-8",
            )
            stable_html.write_text(
                "<!doctype html><html><head><title>Good</title></head><body><main><section>good root</section></main><script>1</script></body></html>",
                encoding="utf-8",
            )
            bridge = StubBridge(root_html)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch.emit = AsyncMock()
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、定价、故事、门店、联系页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                ],
            )
            orch._stable_preview_path = stable_html
            orch._stable_preview_files = [str(stable_html)]
            orch._stable_preview_stage = "builder_quality_pass"

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                result = asyncio.run(orch._execute_subtask(plan.subtasks[1], plan, "kimi-coding", {}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

            restored = root_html.read_text(encoding="utf-8")

        self.assertFalse(result.get("success"))
        self.assertTrue(
            "Only Builder 1 may write" in result.get("error", "")
            or "quality gate failed" in str(result.get("error", "")).lower()
        )
        self.assertIn("good root", restored)

    def test_builder_prefers_claimed_written_files_over_directory_scan(self):
        class StubBridge:
            config = {}

            def __init__(self, claimed_path: Path):
                self.claimed_path = claimed_path

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({"stage": "builder_write", "path": str(self.claimed_path)})
                return {
                    "success": False,
                    "output": "",
                    "error": "builder execution timeout after 421s.",
                    "tool_results": [],
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            claimed = tmp_out / "about.html"
            unrelated = tmp_out / "pricing.html"
            claimed.write_text(
                "<!doctype html><html><head><title>About</title></head><body><main>about</main></body></html>",
                encoding="utf-8",
            )
            unrelated.write_text(
                "<!doctype html><html><head><title>Pricing</title></head><body><main>pricing</main></body></html>",
                encoding="utf-8",
            )

            bridge = StubBridge(claimed)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch.emit = AsyncMock()
            orch._run_started_at = time.time() - 30
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、定价、故事、门店、联系页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                asyncio.run(orch._execute_subtask(plan.subtasks[1], plan, "kimi-coding", {}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        subtask_complete_calls = [
            call for call in orch.emit.await_args_list
            if call.args and call.args[0] == "subtask_complete"
        ]
        self.assertTrue(subtask_complete_calls)
        files_created = subtask_complete_calls[-1].args[1]["files_created"]
        self.assertEqual(files_created, [orch._normalize_generated_path(str(claimed))])

    def test_builder_timeout_with_valid_saved_artifact_is_salvaged(self):
        class StubBridge:
            def __init__(self, html_path: Path):
                self.config = {}
                self.html_path = html_path

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": False,
                    "output": "",
                    "error": "builder execution timeout after 421s.",
                    "tool_results": [{"written": True, "path": str(self.html_path)}],
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Maison Aurelia</title><style>:root{--bg:#0b1020;--fg:#f3f5f8;--panel:#121a34;--line:rgba(255,255,255,.08);--accent:#d8b36a}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#121a34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;padding:20px 24px}.hero,.grid,.cta{display:grid;gap:18px}.hero{grid-template-columns:1.3fr .7fr;align-items:center}.grid{grid-template-columns:repeat(3,1fr)}.cta{grid-template-columns:repeat(2,1fr)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:24px}button{padding:12px 18px;border:none;border-radius:999px;background:var(--accent);color:#201505;font-weight:700}main{display:grid;gap:18px;padding:24px}@media(max-width:900px){.hero,.grid,.cta{grid-template-columns:1fr}}</style></head>
<body><header><nav><strong>Maison Aurelia</strong><button>Book an appointment</button></nav></header><main><section class="hero"><article class="panel"><h1>Quiet luxury, precisely composed.</h1><p>Maison Aurelia presents a refined luxury narrative with editorial structure, dense content, and a calm premium tone.</p><p>Every section is production-ready and visually complete.</p></article><article class="panel"><h2>Private presentation</h2><p>Discover collections, heritage, and bespoke services.</p></article></section><section class="grid"><article class="panel"><h3>Craft</h3><p>Hand-finished details from the Paris atelier.</p></article><article class="panel"><h3>Materials</h3><p>Rare leathers and precious metal accents.</p></article><article class="panel"><h3>Service</h3><p>Concierge support for collectors worldwide.</p></article></section><section class="cta"><article class="panel"><h3>Visit the maison</h3><p>Private appointments in flagship salons.</p></article><article class="panel"><h3>Request a consultation</h3><p>Receive a curated presentation and next steps.</p></article></section></main><footer class="panel">Maison Aurelia.</footer><script>document.querySelectorAll('button').forEach(btn=>btn.addEventListener('click',()=>{}));</script></body></html>""",
                encoding="utf-8",
            )
            bridge = StubBridge(index)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch.emit = AsyncMock()
            plan = Plan(goal="做一个高端品牌官网", subtasks=[SubTask(id="1", agent_type="builder", description="build")])

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                result = asyncio.run(orch._execute_subtask(plan.subtasks[0], plan, "kimi-coding", {}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("error", ""), "")

    def test_execute_subtask_exception_marks_attempt_failed_before_retry_handler(self):
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
        self.assertIn("failed", statuses)

    def test_execute_subtask_failed_result_marks_attempt_failed_before_retry_handler(self):
        class FailingBridge:
            config = {}

            async def execute(self, **kwargs):
                return {
                    "success": False,
                    "output": "",
                    "error": "builder quality gate failed",
                    "tool_results": [],
                }

        orch = Orchestrator(ai_bridge=FailingBridge(), executor=None)
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch.emit = AsyncMock()
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=2)
        plan = Plan(goal="Build test page", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertIn("failed", statuses)

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

    def test_builder_nav_repair_only_false_when_other_quality_errors_exist(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        error = (
            "Builder quality gate failed (score=64). Errors: "
            "['Some multi-page routes are still too thin / stub-like for shipment: features.html (19855 bytes)', "
            "'index.html does not expose enough working local navigation links to the additional pages.']"
        )

        self.assertFalse(orch._builder_nav_repair_only(error))

    def test_nav_only_builder_retry_preserves_current_output_without_cleanup(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        orch._restore_output_from_stable_preview = Mock(return_value=[])
        orch._cleanup_internal_builder_artifacts = Mock(return_value=["/tmp/evermind_output/index.html"])
        orch._evaluate_multi_page_artifacts = Mock(return_value={
            "ok": False,
            "html_files": ["index.html", "pricing.html", "features.html", "contact.html"],
            "observed_html_files": ["index.html", "pricing.html", "features.html", "contact.html"],
            "invalid_html_files": [],
            "errors": ["index.html does not expose enough working local navigation links to the additional pages."],
            "warnings": [],
            "repair_scope": "root_nav_only",
            "nav_targets": ["contact.html"],
            "matched_nav_targets": ["contact.html"],
            "missing_nav_targets": [],
            "unlinked_secondary_pages": ["pricing.html", "features.html"],
        })

        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium site",
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = (
            "Builder quality gate failed (score=80). Errors: "
            "['index.html does not expose enough working local navigation links to the additional pages.']"
        )
        plan = Plan(goal="做一个四页面官网，包含首页、定价页、功能页和联系页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        orch._restore_output_from_stable_preview.assert_not_called()
        orch._cleanup_internal_builder_artifacts.assert_not_called()
        self.assertIn("NAVIGATION REPAIR ONLY.", captured.get("desc", ""))

    def test_nav_only_builder_retry_prompt_locks_output_to_index_only(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        orch._restore_output_from_stable_preview = Mock(return_value=[])
        orch._cleanup_internal_builder_artifacts = Mock(return_value=[])
        orch._evaluate_multi_page_artifacts = Mock(return_value={
            "ok": False,
            "html_files": ["index.html", "pricing.html", "features.html", "contact.html"],
            "observed_html_files": ["index.html", "pricing.html", "features.html", "contact.html"],
            "invalid_html_files": [],
            "errors": ["index.html references missing local pages: destinations.html"],
            "warnings": [],
            "repair_scope": "root_nav_only",
            "nav_targets": ["pricing.html", "features.html", "contact.html", "destinations.html"],
            "matched_nav_targets": ["pricing.html", "features.html", "contact.html"],
            "missing_nav_targets": ["destinations.html"],
            "unlinked_secondary_pages": [],
        })

        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium site",
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = (
            "Builder quality gate failed (score=70). Errors: "
            "['index.html references missing local pages: destinations.html']"
        )
        plan = Plan(goal="做一个四页面官网，包含首页、定价页、功能页和联系页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        desc = captured.get("desc", "")
        self.assertIn(orchestrator_module.BUILDER_NAV_REPAIR_ONLY_MARKER, desc)
        self.assertIn(f"{orchestrator_module.BUILDER_TARGET_OVERRIDE_MARKER} index.html", desc)
        self.assertIn("Output ONLY a single fenced ```html index.html ...``` block", desc)
        self.assertIn("Do NOT write styles.css, app.js, or any secondary HTML page during this retry.", desc)
        self.assertIn("Do NOT return prose, explanations, summaries, or planning text", desc)

    def test_retry_prompt_for_builder_thin_page_and_nav_uses_direct_multifile_targets(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html."
            ),
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = (
            "Builder quality gate failed (score=64). Errors: "
            "['Some multi-page routes are still too thin / stub-like for shipment: features.html (19855 bytes)', "
            "'index.html does not expose enough working local navigation links to the additional pages.']"
        )
        plan = Plan(goal="创建一个介绍美国旅游景点的网站，网站一共8页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        self.assertIn("DIRECT MULTI-FILE DELIVERY ONLY.", captured.get("desc", ""))
        self.assertIn("features.html", captured.get("desc", ""))
        self.assertNotIn("NAVIGATION REPAIR ONLY.", captured.get("desc", ""))

    def test_handle_failure_failed_retry_requeues_when_budget_remains(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="builder", description="build landing page", depends_on=[], max_retries=3)
        subtask.status = TaskStatus.FAILED
        subtask.error = "Builder quality gate failed"
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        async def fake_execute_subtask(st, _plan, _model, _results):
            return {"success": False, "error": "Builder quality gate failed again"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertFalse(ok)
        self.assertEqual(subtask.status, TaskStatus.PENDING)
        self.assertEqual(subtask.retries, 1)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertEqual(statuses[0], "running")
        self.assertNotIn("failed", statuses)

    def test_execute_plan_keeps_subtask_runnable_when_retry_is_scheduled(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="builder", description="build landing page", depends_on=[], max_retries=2)
        plan = Plan(goal="Build premium website", subtasks=[subtask])
        attempts = {"count": 0}

        async def fake_execute_subtask(st, _plan, _model, _results):
            attempts["count"] += 1
            if attempts["count"] == 1:
                st.error = "Builder quality gate failed"
                return {"success": False, "error": st.error}
            st.status = TaskStatus.COMPLETED
            st.output = "ok"
            return {"success": True, "output": "ok"}

        async def fake_handle_failure(st, _plan, _model, _results):
            st.retries += 1
            st.status = TaskStatus.PENDING
            return False

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]
        orch._handle_failure = fake_handle_failure  # type: ignore[method-assign]

        results = asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertEqual(attempts["count"], 2)
        self.assertTrue(results["1"]["success"])
        self.assertEqual(subtask.status, TaskStatus.COMPLETED)

    def test_execute_plan_runs_soft_dependency_downstream_after_optional_failure(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()

        imagegen = SubTask(id="1", agent_type="imagegen", description="draw assets", depends_on=[], max_retries=0)
        builder = SubTask(id="2", agent_type="builder", description="build game", depends_on=["1"], max_retries=0)
        reviewer = SubTask(id="3", agent_type="reviewer", description="review", depends_on=["2"], max_retries=0)
        plan = Plan(goal="Build a 3D web game", subtasks=[imagegen, builder, reviewer])
        calls = []

        async def fake_execute_subtask(st, _plan, _model, _results):
            calls.append(st.id)
            if st.id == "1":
                st.error = "imagegen execution timeout after 242s."
                return {"success": False, "error": st.error}
            st.status = TaskStatus.COMPLETED
            st.output = f"ok-{st.id}"
            return {"success": True, "output": st.output}

        async def fake_handle_failure(st, _plan, _model, _results):
            st.status = TaskStatus.FAILED
            st.error = str(st.error or "failed").strip()
            return False

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]
        orch._handle_failure = fake_handle_failure  # type: ignore[method-assign]

        results = asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertEqual(calls, ["1", "2", "3"])
        self.assertEqual(imagegen.status, TaskStatus.FAILED)
        self.assertEqual(builder.status, TaskStatus.COMPLETED)
        self.assertEqual(reviewer.status, TaskStatus.COMPLETED)
        self.assertTrue(results["2"]["success"])
        self.assertTrue(results["3"]["success"])

    def test_analyst_gate_requires_two_live_reference_urls(self):
        """Analyst gate hard-fails when no live references were browsed."""
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

        self.assertFalse(result.get("success"))
        self.assertIn("at least 2 live reference URLs", str(result.get("error", "")))

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
        self.assertIn("MUST use the browser tool on at least 2 different source URLs", captured.get("desc", ""))
        self.assertIn("<reference_sites>", captured.get("desc", ""))


class TestFinalPreviewEmission(unittest.TestCase):
    def test_select_preview_artifact_prefers_root_index_over_task_local_index(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            task_html = tmp_out / "task_3" / "index.html"
            task_html.parent.mkdir(parents=True, exist_ok=True)
            root_html.write_text("<!doctype html><html><body>root</body></html>", encoding="utf-8")
            task_html.write_text("<!doctype html><html><body>task</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                selected = orch._select_preview_artifact_for_files([
                    str(task_html),
                    str(root_html),
                ])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(selected, root_html)

    def test_select_preview_artifact_ignores_stable_preview_snapshots(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            task_html = tmp_out / "task_5" / "index.html"
            stable_html = tmp_out / "_stable_previews" / "run_prev" / "approved_task_1" / "index.html"
            task_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            task_html.write_text("<!doctype html><html><body>task fallback</body></html>", encoding="utf-8")
            stable_html.write_text("<!doctype html><html><body>stable snapshot</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                selected = orch._select_preview_artifact_for_files([
                    str(task_html),
                    str(stable_html),
                ])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(selected, task_html)

    def test_current_run_html_artifacts_ignore_internal_preview_artifacts(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            task_html = tmp_out / "task_5" / "index.html"
            stable_html = tmp_out / "_stable_previews" / "run_prev" / "approved_task_1" / "index.html"
            task_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            root_html.write_text("<!doctype html><html><body>root</body></html>", encoding="utf-8")
            task_html.write_text("<!doctype html><html><body>task fallback</body></html>", encoding="utf-8")
            stable_html.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")

            orch._run_started_at = time.time()
            now = time.time()
            os.utime(root_html, (now, now))
            os.utime(task_html, (now, now))
            os.utime(stable_html, (now, now))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                html_files = orch._current_run_html_artifacts()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(html_files, [root_html])

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

    def test_emit_final_preview_prefers_stable_snapshot_over_newer_run_local_artifact(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            failed_html = tmp_out / "task_3" / "index.html"
            failed_html.parent.mkdir(parents=True, exist_ok=True)
            failed_html.write_text("<!doctype html><html><body>bad</body></html>", encoding="utf-8")

            stable_html = tmp_out / "_stable_previews" / "run_test" / "approved_task_2" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text("<!doctype html><html><body>good</body></html>", encoding="utf-8")

            now = time.time()
            os.utime(failed_html, (now + 1, now + 1))
            os.utime(stable_html, (now, now))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                orch._stable_preview_path = stable_html
                orch._stable_preview_files = [str(stable_html)]
                orch._stable_preview_stage = "builder_quality_pass"
                asyncio.run(orch._emit_final_preview())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/_stable_previews/run_test/approved_task_2/index.html", preview_events[0].get("preview_url", ""))
        self.assertTrue(preview_events[0].get("stable_preview"))

    def test_emit_final_preview_failed_run_restores_stable_root_and_skips_failed_artifact(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            root_html.write_text("<!doctype html><html><body>broken</body></html>", encoding="utf-8")

            failed_html = tmp_out / "task_3" / "index.html"
            failed_html.parent.mkdir(parents=True, exist_ok=True)
            failed_html.write_text("<!doctype html><html><body>bad</body></html>", encoding="utf-8")

            stable_html = tmp_out / "_stable_previews" / "run_test" / "approved_task_2" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text("<!doctype html><html><body>good</body></html>", encoding="utf-8")

            now = time.time()
            os.utime(root_html, (now + 1, now + 1))
            os.utime(failed_html, (now + 1, now + 1))
            os.utime(stable_html, (now, now))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                orch._stable_preview_path = stable_html
                orch._stable_preview_files = [str(stable_html)]
                orch._stable_preview_stage = "builder_quality_pass"
                asyncio.run(orch._emit_final_preview(report_success=False))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertEqual(root_html.read_text(encoding="utf-8"), stable_html.read_text(encoding="utf-8"))

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/_stable_previews/run_test/approved_task_2/index.html", preview_events[0].get("preview_url", ""))
        self.assertTrue(preview_events[0].get("stable_preview"))

    def test_emit_final_preview_failed_run_without_stable_snapshot_keeps_live_root_and_emits_preview(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            root_html.write_text(
                (
                    "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<title>Fallback</title>"
                    "<style>"
                    ":root{color-scheme:light}body{margin:0;font-family:'Noto Sans SC',sans-serif;background:#08111f;color:#f3f1ea}"
                    "main{display:grid;gap:24px;padding:48px}section{padding:28px;border:1px solid rgba(255,255,255,.12);border-radius:24px}"
                    "@media (max-width: 720px){main{padding:24px}}"
                    "</style></head><body><main>"
                    "<section><h1>Fallback Preview</h1><p>"
                    + ("keep live root " * 90)
                    + "</p></section>"
                    "<section><h2>Highlights</h2><p>"
                    + ("cinematic travel story " * 60)
                    + "</p></section>"
                    "</main><script>window.__fallbackPreview=true;</script></body></html>"
                ),
                encoding="utf-8",
            )

            now = time.time()
            os.utime(root_html, (now + 1, now + 1))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview(report_success=False))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(root_html.exists())

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/index.html", preview_events[0].get("preview_url", ""))
        self.assertFalse(preview_events[0].get("stable_preview"))
        self.assertEqual(preview_events[0].get("stage"), "failed_run_live_fallback")

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

    def test_emit_final_preview_promotes_shared_assets_into_stable_snapshot(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index_html = tmp_out / "index.html"
            styles_css = tmp_out / "styles.css"
            app_js = tmp_out / "app.js"
            index_html.write_text(
                "<!doctype html><html><head><link rel='stylesheet' href='styles.css'></head>"
                "<body><script src='app.js'></script></body></html>",
                encoding="utf-8",
            )
            styles_css.write_text("body{background:#f4efe6;color:#1d1c1a;}", encoding="utf-8")
            app_js.write_text("window.__previewReady=true;", encoding="utf-8")

            now = time.time()
            fresh = now + 1
            for item in (index_html, styles_css, app_js):
                os.utime(item, (fresh, fresh))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview(report_success=True))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            stable_root = tmp_out / "_stable_previews"
            snapshots = [path for path in stable_root.rglob("*") if path.is_dir()]
            self.assertTrue(any((path / "styles.css").exists() for path in snapshots))
            self.assertTrue(any((path / "app.js").exists() for path in snapshots))

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertTrue(preview_events[0].get("stable_preview"))


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
        self.assertEqual(reviewer.status, TaskStatus.BLOCKED)
        self.assertEqual(deployer.status, TaskStatus.BLOCKED)
        self.assertEqual(tester.status, TaskStatus.BLOCKED)


class TestDebuggerNoop(unittest.TestCase):
    def test_debugger_noops_when_reviewer_and_tester_already_passed(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        reviewer = SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1"])
        tester = SubTask(id="4", agent_type="tester", description="test", depends_on=["3"])
        debugger = SubTask(id="5", agent_type="debugger", description="debug", depends_on=["4"])
        plan = Plan(
            goal="做一个高端多页面旅游官网",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                reviewer,
                tester,
                debugger,
            ],
        )
        prev_results = {
            "3": {
                "success": True,
                "output": json.dumps({
                    "verdict": "APPROVED",
                    "scores": {
                        "layout": 8,
                        "color": 8,
                        "typography": 8,
                        "animation": 7,
                        "responsive": 8,
                        "functionality": 8,
                        "completeness": 8,
                        "originality": 7,
                    },
                    "blocking_issues": [],
                    "required_changes": [],
                    "missing_deliverables": [],
                }),
                "error": "",
            },
            "4": {
                "success": True,
                "output": json.dumps({"status": "pass", "details": "All required pages verified."}),
                "error": "",
            },
        }

        result = asyncio.run(orch._execute_subtask(debugger, plan, "kimi-coding", prev_results=prev_results))

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("mode"), "debugger_noop")
        self.assertEqual(result.get("files_created"), [])
        self.assertIn("不做额外改写", result.get("output", ""))
        self.assertEqual(debugger.status, TaskStatus.COMPLETED)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "debugger_noop"
            for call in orch.emit.await_args_list
        ))

    def test_execute_plan_reviewer_requeue_clears_progress_high_water_and_resets_nodes(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._progress_high_water = {"1": 100, "2": 100, "4": 100}

        builder_a = SubTask(id="1", agent_type="builder", description="build top", depends_on=[], max_retries=1)
        builder_b = SubTask(id="2", agent_type="builder", description="build bottom", depends_on=[], max_retries=1)
        reviewer = SubTask(id="4", agent_type="reviewer", description="review", depends_on=["1", "2"], max_retries=0)
        downstream = SubTask(id="5", agent_type="tester", description="test", depends_on=["4"], max_retries=0)
        plan = Plan(goal="Build premium website", subtasks=[builder_a, builder_b, reviewer, downstream])
        attempts = {"4": 0}

        async def fake_execute_subtask(st, _plan, _model, results):
            if st.id == "1":
                st.status = TaskStatus.COMPLETED
                st.output = "builder-a-ok"
                return {"success": True, "output": st.output}
            if st.id == "2":
                st.status = TaskStatus.COMPLETED
                st.output = "builder-b-ok"
                return {"success": True, "output": st.output}
            if st.id == "4":
                attempts["4"] += 1
                if attempts["4"] == 1:
                    return {"success": False, "requeue_requested": True, "requeue_subtasks": ["1", "2", "4"]}
                st.status = TaskStatus.COMPLETED
                st.output = "review-ok"
                return {"success": True, "output": st.output}
            st.status = TaskStatus.COMPLETED
            st.output = f"ok-{st.id}"
            return {"success": True, "output": st.output}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        results = asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertEqual(attempts["4"], 2)
        self.assertTrue(results["5"]["success"])
        self.assertEqual(orch._progress_high_water, {})
        self.assertEqual(builder_a.status, TaskStatus.COMPLETED)
        self.assertEqual(builder_b.status, TaskStatus.COMPLETED)
        self.assertEqual(reviewer.status, TaskStatus.COMPLETED)
        self.assertEqual(downstream.status, TaskStatus.COMPLETED)

        queued_calls = [
            call for call in orch._sync_ne_status.await_args_list
            if len(call.args) >= 2 and call.args[1] == "queued"
        ]
        self.assertEqual([call.args[0] for call in queued_calls], ["1", "2", "4", "5"])
        for call in queued_calls:
            self.assertEqual(call.kwargs.get("progress"), 0)
            self.assertEqual(call.kwargs.get("phase"), "requeued")
            self.assertEqual(call.kwargs.get("output_summary"), "")
            self.assertEqual(call.kwargs.get("error_message"), "")
            self.assertTrue(call.kwargs.get("reset_started_at"))

        requeue_events = [
            call.args[1] for call in orch.emit.await_args_list
            if len(call.args) >= 2
            and call.args[0] == "subtask_progress"
            and isinstance(call.args[1], dict)
            and call.args[1].get("stage") == "requeue_downstream"
        ]
        self.assertEqual(len(requeue_events), 1)
        self.assertEqual(requeue_events[0].get("requeue_subtasks"), ["1", "2", "4", "5"])

    def test_parallel_website_builder_failure_blocks_downstream(self):
        """A failed parallel builder must block reviewer/deployer/tester; no degraded pass-through."""
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

        self.assertEqual(bridge.calls, ["builder", "builder"])
        reviewer = next(st for st in plan.subtasks if st.id == "4")
        deployer = next(st for st in plan.subtasks if st.id == "5")
        tester = next(st for st in plan.subtasks if st.id == "6")
        self.assertEqual(reviewer.status, TaskStatus.BLOCKED)
        self.assertEqual(deployer.status, TaskStatus.BLOCKED)
        self.assertEqual(tester.status, TaskStatus.BLOCKED)

    def test_parallel_nonwebsite_builder_failure_blocks_even_with_preview(self):
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

        self.assertEqual(bridge.calls, ["builder", "builder"])
        reviewer = next(st for st in plan.subtasks if st.id == "4")
        deployer = next(st for st in plan.subtasks if st.id == "5")
        tester = next(st for st in plan.subtasks if st.id == "6")
        self.assertEqual(reviewer.status, TaskStatus.BLOCKED)
        self.assertEqual(deployer.status, TaskStatus.BLOCKED)
        self.assertEqual(tester.status, TaskStatus.BLOCKED)


class TestRetryFromFailureStateReset(unittest.TestCase):
    def test_retry_from_failure_requeues_downstream_tasks(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "<!DOCTYPE html><html><head></head><body>fixed</body></html>"}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._progress_high_water = {"2": 100, "3": 100}

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
        self.assertEqual(orch._progress_high_water, {})

        queued_calls = [
            call for call in orch._sync_ne_status.await_args_list
            if len(call.args) >= 2 and call.args[1] == "queued"
        ]
        self.assertEqual([call.args[0] for call in queued_calls], ["2", "3"])
        for call in queued_calls:
            self.assertEqual(call.kwargs.get("progress"), 0)
            self.assertEqual(call.kwargs.get("phase"), "requeued")
            self.assertEqual(call.kwargs.get("output_summary"), "")
            self.assertEqual(call.kwargs.get("error_message"), "")
            self.assertTrue(call.kwargs.get("reset_started_at"))

        requeue_events = [
            call.args[1] for call in orch.emit.await_args_list
            if len(call.args) >= 2
            and call.args[0] == "subtask_progress"
            and isinstance(call.args[1], dict)
            and call.args[1].get("stage") == "requeue_downstream"
        ]
        self.assertEqual(len(requeue_events), 1)
        self.assertEqual(requeue_events[0].get("requeue_subtasks"), ["2", "3"])


class TestStablePreviewPreservation(unittest.TestCase):
    def test_prepare_output_dir_for_run_preserves_stable_previews(self):
        orch = Orchestrator(ai_bridge=None, executor=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            stable_html = tmp_out / "_stable_previews" / "run_prev" / "final_success_task_final" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")
            failed_html = tmp_out / "task_9" / "index.html"
            failed_html.parent.mkdir(parents=True, exist_ok=True)
            failed_html.write_text("<!doctype html><html><body>failed</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._prepare_output_dir_for_run()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(stable_html.exists())
            self.assertFalse(failed_html.exists())

    def test_prepare_output_dir_for_run_removes_stale_root_assets_and_temp_images(self):
        orch = Orchestrator(ai_bridge=None, executor=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            stable_html = tmp_out / "_stable_previews" / "run_prev" / "final_success_task_final" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")
            (tmp_out / "assets").mkdir(parents=True, exist_ok=True)
            (tmp_out / "assets" / "old.png").write_text("x", encoding="utf-8")
            (tmp_out / "browser_records").mkdir(parents=True, exist_ok=True)
            (tmp_out / "browser_records" / "trace.json").write_text("{}", encoding="utf-8")
            (tmp_out / "_builder_backups").mkdir(parents=True, exist_ok=True)
            (tmp_out / "_builder_backups" / "index.bak").write_text("backup", encoding="utf-8")
            (tmp_out / "tmpqtcfo9rf.png").write_text("image", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._prepare_output_dir_for_run()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(stable_html.exists())
            self.assertFalse((tmp_out / "assets").exists())
            self.assertFalse((tmp_out / "browser_records").exists())
            self.assertFalse((tmp_out / "_builder_backups").exists())
            self.assertFalse((tmp_out / "tmpqtcfo9rf.png").exists())

    def test_builder_browser_is_suppressed_when_upstream_handoff_exists(self):
        class StubBridge:
            def __init__(self):
                self.config = {"builder": {"enable_browser_search": True}}
                self.seen_plugins = []

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.seen_plugins = [getattr(plugin, "name", "") for plugin in plugins]
                return {
                    "success": True,
                    "output": """```html index.html
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'></head><body><main><section><h1>Maison</h1><p>Luxury site</p></section></main></body></html>
```""",
                    "tool_results": [],
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch.emit = AsyncMock()
        plan = Plan(
            goal="做一个品牌官网首页",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="uidesign", description="design"),
                SubTask(id="3", agent_type="scribe", description="content"),
                SubTask(id="4", agent_type="builder", description="build", depends_on=["1", "2", "3"]),
            ],
        )
        prev_results = {
            "1": {"output": "analyst handoff"},
            "2": {"output": "ui handoff"},
            "3": {"output": "scribe handoff"},
        }

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                preview_validation_module.OUTPUT_DIR = Path(td)
                result = asyncio.run(orch._execute_subtask(plan.subtasks[3], plan, "kimi-coding", prev_results))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn("file_ops", bridge.seen_plugins)
        self.assertNotIn("browser", bridge.seen_plugins)

    def test_multi_page_builder_prompt_includes_assigned_html_filenames(self):
        class StubBridge:
            def __init__(self):
                self.config = {}
                self.seen_input = ""

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.seen_input = str(input_data or "")
                return {"success": False, "output": "", "error": "stop", "tool_results": []}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch.emit = AsyncMock()
        plan = Plan(
            goal="做一个8页奢侈品品牌官网",
            subtasks=[
                SubTask(id="4", agent_type="builder", description="build primary", depends_on=[]),
                SubTask(id="5", agent_type="builder", description="build secondary", depends_on=[]),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                preview_validation_module.OUTPUT_DIR = Path(td)
                asyncio.run(orch._execute_subtask(plan.subtasks[0], plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(
            "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, solutions.html.",
            bridge.seen_input,
        )
        self.assertIn(
            "This builder run is in DIRECT MULTI-FILE DELIVERY mode.",
            bridge.seen_input,
        )
        self.assertIn(
            "Return fenced HTML blocks for the assigned filenames directly in the model response.",
            bridge.seen_input,
        )
        self.assertIn(
            "A single-page draft is considered incomplete delivery.",
            bridge.seen_input,
        )


class TestBuilderBootstrapScaffold(unittest.TestCase):
    def test_single_builder_multi_page_gets_full_target_set_and_scaffold(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="做一个介绍奢侈品的英文网站（8页），页面要简约高级，像苹果一样",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build the full premium multi-page experience",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                targets = orch._builder_bootstrap_targets(plan, plan.subtasks[0])
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[0])
                files_exist = all((tmp_out / name).exists() for name in targets)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertEqual(len(targets), 8)
        self.assertEqual(targets[0], "index.html")
        self.assertEqual(targets[1:4], ["pricing.html", "features.html", "solutions.html"])
        self.assertEqual(len(written), 8)
        self.assertTrue(files_exist)

    def test_single_builder_travel_multi_page_uses_travel_fallback_routes(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="创建一个介绍美国旅游景点的 8 页网站，详细介绍加州所有比较好玩的景点和旅行攻略",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build the full California travel multi-page experience",
                ),
            ],
        )

        targets = orch._builder_bootstrap_targets(plan, plan.subtasks[0])

        self.assertEqual(len(targets), 8)
        self.assertEqual(
            targets[:5],
            ["index.html", "attractions.html", "cities.html", "nature.html", "coast.html"],
        )
        self.assertNotIn("pricing.html", targets[:6])

    def test_multi_page_builder_seeds_internal_scaffold_without_counting_as_preview(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, input_data):
                text = str(input_data or "")
                if "index.html" in text:
                    return ["index.html", "brand.html", "craftsmanship.html", "collections.html"]
                return ["materials.html", "heritage.html", "boutiques.html", "contact.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="制作一个8页奢侈品网站",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="must create /tmp/evermind_output/index.html and fallback set: brand.html, craftsmanship.html, collections.html",
                ),
                SubTask(
                    id="5",
                    agent_type="builder",
                    description="fallback set: materials.html, heritage.html, boutiques.html, contact.html",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[0])
                current = orch._current_run_html_artifacts()
                exists = (tmp_out / "index.html").exists()
                is_bootstrap = preview_validation_module.is_bootstrap_html_artifact(tmp_out / "index.html")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(str(tmp_out / "index.html"), written)
        self.assertTrue(exists)
        self.assertTrue(is_bootstrap)
        self.assertEqual(current, [])

    def test_multi_page_builder_reseeds_corrupted_assigned_page(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, input_data):
                return ["index.html", "pricing.html", "features.html", "contact.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="制作一个4页奢侈品网站",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build index.html, pricing.html, features.html, contact.html",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            broken = tmp_out / "pricing.html"
            broken.write_text(
                "<!DOCTYPE html><html><head><style>body{opacity:1}... [TRUNCATED]",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[0])
                rewritten = broken.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(str(broken), written)
        self.assertIn("evermind-bootstrap", rewritten)

    def test_secondary_builder_never_seeds_index_even_if_description_mentions_it(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, input_data):
                text = str(input_data or "")
                if "primary builder" in text:
                    return ["index.html", "brand.html", "craftsmanship.html", "collections.html"]
                return ["index.html", "materials.html", "heritage.html", "contact.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="制作一个8页奢侈品网站",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="primary builder must create /tmp/evermind_output/index.html plus brand.html, craftsmanship.html, collections.html",
                ),
                SubTask(
                    id="5",
                    agent_type="builder",
                    description="secondary builder got a noisy handoff that also mentions index.html alongside materials.html, heritage.html, contact.html",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                primary_targets = orch._builder_bootstrap_targets(plan, plan.subtasks[0])
                secondary_targets = orch._builder_bootstrap_targets(plan, plan.subtasks[1])
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[1])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertEqual(primary_targets[0], "index.html")
        self.assertNotIn("index.html", secondary_targets)
        self.assertEqual(len(secondary_targets), 4)
        self.assertNotIn(str(tmp_out / "index.html"), written)
        self.assertTrue(all(Path(item).name != "index.html" for item in written))

    def test_goal_language_mismatch_page_is_not_preserved_over_scaffold(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="做一个介绍奢侈品的英文网站（8页），页面要像苹果官网一样高级",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build the full premium multi-page experience",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index_path = tmp_out / "index.html"
            index_path.write_text(
                "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"><title>器之道</title><style>body{font-family:sans-serif}main{padding:40px}</style></head><body><main><section><h1>器之道</h1><p>东方工艺美学与品牌故事。</p></section><section><h2>系列</h2><p>器物、匠心、传承。</p></section><script>console.log('ok')</script></main></body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[0])
                rewritten = index_path.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(str(index_path), written)
        self.assertIn("evermind-bootstrap scaffold", rewritten.lower())


class TestBuilderDiskScanIsolation(unittest.TestCase):
    def test_secondary_builder_disk_scan_only_collects_assigned_pages(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, input_data):
                text = str(input_data or "")
                if "primary builder" in text:
                    return ["index.html", "pricing.html", "features.html", "solutions.html"]
                return ["platform.html", "contact.html", "about.html", "faq.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="制作一个8页奢侈品网站",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="primary builder owns index.html, pricing.html, features.html, solutions.html",
                ),
                SubTask(
                    id="5",
                    agent_type="builder",
                    description="secondary builder owns platform.html, contact.html, about.html, faq.html",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text("<html><body>home</body></html>", encoding="utf-8")
                (tmp_out / "contact.html").write_text("<html><body>contact</body></html>", encoding="utf-8")
                found = orch._collect_recent_builder_disk_scan_files(
                    plan,
                    plan.subtasks[1],
                    scan_cutoff=time.time() - 10,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn(str(tmp_out / "contact.html"), found)
        self.assertNotIn(str(tmp_out / "index.html"), found)


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


class TestBuilderPostWriteIdleTimeout(unittest.TestCase):
    def test_normalize_html_artifact_closes_unterminated_style_before_quality_gate(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        raw = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Voxel</title>
  <style>
    body { margin: 0; background: #111; color: #fff; }
    .hud { display: flex; gap: 12px; }
<body>
  <main class="hud"><section>Ready</section></main>
</html>"""

        fixed = orch._normalize_html_artifact(raw)

        self.assertIn("</style>", fixed.lower())
        self.assertIn("</body>", fixed.lower())
        self.assertTrue(fixed.lower().index("</style>") < fixed.lower().index("</body>"))

    def test_normalize_html_artifact_inserts_missing_head_close_before_body(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        raw = """<!DOCTYPE html>
<html>
<head>
  <title>Broken</title>
  <style>body{background:#111;color:#fff}
<body>
  <main><h1>Ready</h1></main>
</html>"""

        fixed = orch._normalize_html_artifact(raw)

        self.assertIn("</head>", fixed.lower())
        self.assertTrue(fixed.lower().index("</head>") < fixed.lower().index("<body"))
        self.assertIn("</style>", fixed.lower())

    def test_builder_post_write_idle_timeout_salvages_valid_written_artifact(self):
        class StubBridge:
            def __init__(self, output_dir: Path):
                self.config = {}
                self.output_dir = output_dir

            async def execute(self, node, plugins, input_data, model, on_progress):
                index = self.output_dir / "index.html"
                index.write_text(
                    """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Saved Before Stall</title><style>:root{--bg:#0b1020;--fg:#e9ecf1;--panel:#121a34;--line:rgba(255,255,255,.08)}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#121a34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;padding:18px 24px;border-bottom:1px solid var(--line)}main{display:grid;gap:18px;padding:24px}.hero,.grid,.cta{display:grid;gap:16px}.hero{grid-template-columns:1.2fr .8fr}.grid{grid-template-columns:repeat(3,1fr)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}button{padding:12px 18px;border:none;border-radius:999px;background:#7bdcff;color:#03253a;font-weight:700}@media(max-width:900px){.hero,.grid,.cta{grid-template-columns:1fr}}</style></head>
<body><header><nav><strong>Northstar</strong><button>Start</button></nav></header><main><section class="hero"><article class="panel"><h1>Saved artifact</h1><p>This page was fully written before the model stalled, so the orchestrator should salvage it instead of waiting indefinitely.</p><p>It includes enough structure, text, and styling to pass the basic quality gate.</p></article><article class="panel"><h2>Status</h2><p>Waiting on idle timeout.</p></article></section><section class="grid"><article class="panel"><h3>One</h3><p>Alpha</p></article><article class="panel"><h3>Two</h3><p>Beta</p></article><article class="panel"><h3>Three</h3><p>Gamma</p></article></section><section class="cta"><article class="panel"><p>Call to action</p></article></section></main><footer>Footer</footer><script>document.querySelector('button').addEventListener('click',()=>{});</script></body></html>""",
                    encoding="utf-8",
                )
                await on_progress({"stage": "builder_write", "path": str(index)})
                await asyncio.sleep(12)
                return {"success": True, "output": "late text", "tool_results": []}

        async def _noop(_evt):
            return None

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            bridge = StubBridge(out)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch.on_event = _noop
            plan = Plan(goal="做一个单页面官网", subtasks=[SubTask(id="1", agent_type="builder", description="build")])
            builder = plan.subtasks[0]
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            original_idle_timeout = orchestrator_module.BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                orchestrator_module.BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC = 5
                with patch.object(orch, "_configured_progress_heartbeat", return_value=5):
                    result = asyncio.run(orch._execute_subtask(builder, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output
                orchestrator_module.BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC = original_idle_timeout

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("error"), "")


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

    def test_game_reviewer_passes_with_browser_use_only_evidence(self):
        class StubBridge:
            def __init__(self):
                self.config = {"qa_enable_browser_use": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "plugin": "browser_use",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap111",
                        "recording_path": "/tmp/reviewer.webm",
                        "capture_path": "/tmp/shot1.png",
                    },
                    {
                        "stage": "browser_action",
                        "plugin": "browser_use",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "click222",
                        "previous_state_hash": "snap111",
                        "state_changed": True,
                        "recording_path": "/tmp/reviewer.webm",
                    },
                    {
                        "stage": "browser_action",
                        "plugin": "browser_use",
                        "action": "press_sequence",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "press333",
                        "previous_state_hash": "click222",
                        "state_changed": True,
                        "keys_count": 4,
                        "recording_path": "/tmp/reviewer.webm",
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\",\"average\":8.2,\"blocking_issues\":[],\"required_changes\":[]}",
                    "tool_results": [{"success": True, "data": {"recording_path": "/tmp/reviewer.webm"}}],
                    "tool_call_stats": {"browser_use": 1},
                    "qa_browser_use_available": True,
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个 3D 枪战网页游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
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

        with patch.object(
            orch,
            "_run_reviewer_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("requeue_requested"))
        self.assertEqual(result.get("requeue_subtasks"), ["1", "2"])
        self.assertEqual(builder.status, TaskStatus.PENDING)
        self.assertEqual(builder.retries, 1)
        self.assertEqual(reviewer.status, TaskStatus.PENDING)

    def test_reviewer_requeue_restores_latest_stable_preview_before_builder_retry(self):
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
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.9,\"improvements\":[\"Restore all missing pages\"]}",
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

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_root = out / "_stable_previews" / "run_1" / "snapshot"
            stable_root.mkdir(parents=True, exist_ok=True)
            stable_index = stable_root / "index.html"
            stable_index.write_text("<!doctype html><html><body>stable home</body></html>", encoding="utf-8")
            (stable_root / "about.html").write_text("<!doctype html><html><body>stable about</body></html>", encoding="utf-8")
            (out / "index.html").write_text("<!doctype html><html><body>broken home</body></html>", encoding="utf-8")
            (out / "pricing.html").write_text("<!doctype html><html><body>stale extra file</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                orch._stable_preview_path = stable_index
                with patch.object(
                    orch,
                    "_run_reviewer_visual_gate",
                    new=AsyncMock(
                        return_value={
                            "ok": True,
                            "errors": [],
                            "warnings": [],
                            "preview_url": "http://127.0.0.1:8765/preview/index.html",
                            "smoke": {"status": "pass"},
                        }
                    ),
                ):
                    result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output
            restored_index = (out / "index.html").read_text(encoding="utf-8")
            restored_about_exists = (out / "about.html").exists()
            restored_pricing_exists = (out / "pricing.html").exists()

        self.assertTrue(result.get("requeue_requested"))
        self.assertEqual(restored_index, "<!doctype html><html><body>stable home</body></html>")
        self.assertTrue(restored_about_exists)
        self.assertFalse(restored_pricing_exists)

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

        with patch.object(
            orch,
            "_run_reviewer_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("requeue_requested"))
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("requeue_subtasks"), ["1", "2", "4"])
        self.assertEqual(builder_a.status, TaskStatus.PENDING)
        self.assertEqual(builder_b.status, TaskStatus.PENDING)
        self.assertEqual(builder_a.retries, 1)
        self.assertEqual(builder_b.retries, 1)

    def test_reviewer_rejection_appends_rework_brief_to_activity_logs(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": (
                        "{\"verdict\":\"REJECTED\",\"blocking_issues\":[\"Primary CTA is weak\"],"
                        "\"required_changes\":[\"Rewrite the hero CTA and add stronger proof blocks\"]}"
                    ),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch._append_ne_activity = MagicMock()

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个产品官网", difficulty="standard", subtasks=[builder, reviewer])

        with patch.object(
            orch,
            "_run_reviewer_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("requeue_requested"))
        messages = [call.args[1] for call in orch._append_ne_activity.call_args_list if len(call.args) >= 2]
        self.assertTrue(any("Reviewer 退回说明" in msg for msg in messages))
        self.assertTrue(any("收到 Reviewer 退回 brief" in msg for msg in messages))

    def test_pro_reviewer_rejected_becomes_non_retryable_failure_when_budget_used(self):
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

        with patch.object(
            orch,
            "_run_reviewer_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("requeue_requested", False))
        # Reviewer rejected and budget exhausted: website tasks now hard-fail
        self.assertFalse(result.get("success"))
        self.assertFalse(result.get("retryable", True))

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

    def test_reviewer_website_requires_bottom_observation_after_scrolling(self):
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
                        "scroll_y": 0,
                        "viewport_height": 900,
                        "page_height": 2600,
                    },
                    {
                        "stage": "browser_action",
                        "action": "scroll",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "scroll222",
                        "state_changed": True,
                        "scroll_y": 1700,
                        "viewport_height": 900,
                        "page_height": 2600,
                        "at_bottom": True,
                        "is_scrollable": True,
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "target": "text=Get Started",
                        "state_hash": "click333",
                        "previous_state_hash": "scroll222",
                        "state_changed": True,
                        "scroll_y": 120,
                        "viewport_height": 900,
                        "page_height": 2600,
                    },
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap444",
                        "previous_state_hash": "click333",
                        "state_changed": True,
                        "scroll_y": 120,
                        "viewport_height": 900,
                        "page_height": 2600,
                        "at_page_bottom": False,
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
        self.assertIn("bottom-of-page", str(result.get("error", "")))

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

    def test_reviewer_tolerates_remote_image_request_failures(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                failed_images = [
                    {"url": "https://images.unsplash.com/photo-1", "error": "net::ERR_FAILED", "resource_type": "image"},
                    {"url": "https://images.unsplash.com/photo-2", "error": "net::ERR_FAILED", "resource_type": "image"},
                    {"url": "https://images.unsplash.com/photo-3", "error": "net::ERR_FAILED", "resource_type": "image"},
                ]
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "observe",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "state_hash": "snap111",
                        "failed_request_count": 3,
                        "recent_failed_requests": failed_images,
                    },
                    {
                        "stage": "browser_action",
                        "action": "scroll",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "state_hash": "scroll222",
                        "previous_state_hash": "snap111",
                        "state_changed": True,
                        "page_height": 2200,
                        "viewport_height": 800,
                        "scroll_y": 1400,
                        "at_bottom": True,
                        "failed_request_count": 3,
                        "recent_failed_requests": failed_images,
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "state_hash": "click333",
                        "previous_state_hash": "scroll222",
                        "state_changed": True,
                        "failed_request_count": 3,
                        "recent_failed_requests": failed_images,
                    },
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "state_hash": "verify444",
                        "previous_state_hash": "click333",
                        "state_changed": False,
                        "page_height": 2200,
                        "viewport_height": 800,
                        "scroll_y": 1400,
                        "at_bottom": True,
                        "failed_request_count": 3,
                        "recent_failed_requests": failed_images,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个高级单页旅游网站", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_reviewer_multi_page_goal_requires_visiting_all_pages(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "is_scrollable": False},
                    {"stage": "browser_action", "action": "act", "subaction": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                    {"stage": "browser_action", "action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
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
        goal = "做一个三页面官网，包含首页、定价页和联系页"
        plan = Plan(goal=goal, subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("visit every requested page", str(result.get("error", "")))

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

        self.assertFalse(result.get("success"))
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

        self.assertFalse(result.get("success"))
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

    def test_tester_game_passes_with_desktop_qa_session_without_browser_tool(self):
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
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        desktop_session = {
            "ok": True,
            "summary": "[Desktop QA Session Evidence]",
            "actions": [
                {
                    "plugin": "desktop_qa_session",
                    "action": "click",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                },
                {
                    "plugin": "desktop_qa_session",
                    "action": "press_sequence",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "keys_count": 5,
                    "state_hash": "game222",
                    "previous_state_hash": "menu111",
                    "state_changed": True,
                },
                {
                    "plugin": "desktop_qa_session",
                    "action": "snapshot",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "game333",
                    "previous_state_hash": "game222",
                    "state_changed": True,
                },
            ],
        }

        with patch.object(orch, "_maybe_collect_desktop_qa_session", new=AsyncMock(return_value=desktop_session)):
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

    def test_reviewer_game_passes_with_desktop_qa_session_without_browser_tool(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "APPROVED",
                        "scores": {
                            "layout": 8,
                            "color": 8,
                            "typography": 8,
                            "animation": 8,
                            "responsive": 8,
                            "functionality": 8,
                            "completeness": 8,
                            "originality": 8,
                        },
                        "issues": [],
                        "blocking_issues": [],
                        "required_changes": [],
                        "missing_deliverables": [],
                        "ship_readiness": 8,
                    }),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        desktop_session = {
            "ok": True,
            "summary": "[Desktop QA Session Evidence]",
            "actions": [
                {"plugin": "desktop_qa_session", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "menu111"},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": True, "url": "http://127.0.0.1:8765/preview/", "keys_count": 4, "state_hash": "game222", "previous_state_hash": "menu111", "state_changed": True},
                {"plugin": "desktop_qa_session", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game333", "previous_state_hash": "game222", "state_changed": True},
            ],
        }

        with patch.object(orch, "_maybe_collect_desktop_qa_session", new=AsyncMock(return_value=desktop_session)):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_reviewer_game_uses_usable_desktop_qa_evidence_even_when_session_not_ok(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_run_smoke": False}
                self.plugins_seen = []

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.plugins_seen = [getattr(plugin, "name", "") for plugin in (plugins or [])]
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "scores": {
                            "layout": 4,
                            "color": 4,
                            "typography": 4,
                            "animation": 3,
                            "responsive": 4,
                            "functionality": 2,
                            "completeness": 3,
                            "originality": 3,
                        },
                        "issues": ["Crash on load"],
                        "blocking_issues": ["Gameplay never initializes"],
                        "required_changes": ["Fix runtime error before review can pass"],
                        "missing_deliverables": [],
                        "ship_readiness": 2,
                    }),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        desktop_session = {
            "ok": False,
            "usable": True,
            "summary": "[Desktop QA Session Evidence]\n- summary: runtime error observed",
            "actions": [
                {"plugin": "desktop_qa_session", "action": "snapshot", "ok": False, "url": "http://127.0.0.1:8765/preview/", "state_hash": "err111"},
                {"plugin": "desktop_qa_session", "action": "click", "ok": False, "url": "http://127.0.0.1:8765/preview/", "state_hash": "err111", "previous_state_hash": "err111"},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": False, "url": "http://127.0.0.1:8765/preview/", "keys_count": 4, "state_hash": "err111", "previous_state_hash": "err111"},
            ],
            "consoleErrors": [{"message": "Crash on load"}],
        }

        with patch.object(orch, "_maybe_collect_desktop_qa_session", new=AsyncMock(return_value=desktop_session)):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        # Reviewer rejected with low scores, but no builder in plan so soft-pass
        self.assertTrue(result.get("success"))
        self.assertNotIn("browser", bridge.plugins_seen)
        self.assertNotIn("browser_use", bridge.plugins_seen)

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


class TestReviewerNonRetryableRejection(unittest.TestCase):
    def test_reviewer_rejection_without_requeue_budget_fails_instead_of_proceeding(self):
        class StubBridge:
            config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "observe",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/index.html",
                    "state_changed": True,
                })
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "issues": ["Mid-page sections are blank"],
                        "required_changes": ["Restore the missing middle sections before approval"],
                    }),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 1},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=1)
        builder.status = TaskStatus.COMPLETED
        builder.retries = builder.max_retries
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个高端多页面官网", subtasks=[builder, reviewer])

        with patch.object(orch, "_interaction_gate_error", return_value=None):
            with patch.object(
                orch,
                "_run_reviewer_visual_gate",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "errors": [],
                        "warnings": [],
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "smoke": {"status": "pass"},
                    }
                ),
                ):
                    result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))

        # Reviewer rejected and builder retries exhausted: website delivery must stop.
        self.assertFalse(result.get("success"))
        self.assertFalse(result.get("retryable", True))
        self.assertIn("blocked until the quality issues are fixed", str(result.get("error", "")))
        self.assertEqual(reviewer.status, TaskStatus.FAILED)

    def test_reviewer_rejection_requeues_transitive_builder_through_polisher(self):
        class StubBridge:
            config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "observe",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/index.html",
                    "state_changed": True,
                })
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "issues": ["导航和图片质量不达标"],
                        "required_changes": ["统一导航结构并修复错图/坏图"],
                    }),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 1},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=2)
        builder.status = TaskStatus.COMPLETED
        polisher = SubTask(id="2", agent_type="polisher", description="polish", depends_on=["1"])
        polisher.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="3", agent_type="reviewer", description="review", depends_on=["2"])
        plan = Plan(goal="做一个高端多页面旅游官网", subtasks=[builder, polisher, reviewer])

        with patch.object(orch, "_interaction_gate_error", return_value=None):
            with patch.object(
                orch,
                "_run_reviewer_visual_gate",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "errors": [],
                        "warnings": [],
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "smoke": {"status": "pass"},
                    }
                ),
            ):
                result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertTrue(result.get("requeue_requested"))
        self.assertIn("1", result.get("requeue_subtasks", []))
        self.assertEqual(builder.status, TaskStatus.PENDING)
        self.assertEqual(reviewer.status, TaskStatus.PENDING)

    def test_collect_transitive_downstream_ids_is_order_independent(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="做一个高端多页面旅游官网",
            subtasks=[
                SubTask(id="6", agent_type="tester", description="test", depends_on=["4", "5"]),
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="7", agent_type="debugger", description="debug", depends_on=["6"]),
                SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["3"]),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["3"]),
                SubTask(id="3", agent_type="polisher", description="polish", depends_on=["1"]),
            ],
        )

        downstream = set(orch._collect_transitive_downstream_ids(plan, ["1"]))

        self.assertEqual(downstream, {"3", "4", "5", "6", "7"})


class TestAnalystHandoffContext(unittest.TestCase):
    def test_builder_context_keeps_curated_image_library_and_skill_plan(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(goal="做一个高端旅游官网", subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[])])
        builder = plan.subtasks[0]
        analyst_output = (
            "<reference_sites>\n- https://example.com\n</reference_sites>\n"
            "<curated_image_library>\n- index.html: use verified West Lake image\n</curated_image_library>\n"
            "<skill_activation_plan>\n- builder_1: apply atlas surface system\n</skill_activation_plan>\n"
            "<builder_1_handoff>\n- Build the homepage\n</builder_1_handoff>\n"
        )

        context = orch._build_analyst_handoff_context(plan, builder, analyst_output)
        self.assertIn("Curated Image Library", context)
        self.assertIn("Skill Activation Plan", context)
        self.assertIn("verified West Lake image", context)


if __name__ == "__main__":
    unittest.main()
