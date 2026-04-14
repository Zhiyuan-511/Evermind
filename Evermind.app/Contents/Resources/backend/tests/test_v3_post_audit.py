import asyncio
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agentic_tools import get_tools_for_role as get_agentic_tool_access
from agentic_tools import tool_bash, tool_file_edit
from ai_bridge import AIBridge
from agentic_runtime import get_tools_for_role
import orchestrator as orchestrator_module
from orchestrator import Orchestrator, Plan, SubTask
from report_generator import ReportGenerator
from task_handoff import HandoffBuilder


class TestAgenticRuntimeDefaults(unittest.TestCase):
    def test_unknown_role_falls_back_to_registered_tools_only(self):
        tools = get_tools_for_role("some-future-role")
        self.assertEqual(tools, ["file_read", "file_list"])

    def test_agentic_tools_unknown_role_uses_safe_default(self):
        tools = get_agentic_tool_access("some-future-role")
        self.assertEqual(sorted(tools.keys()), ["file_list", "file_read"])


class TestAgenticLoopBridgePropagation(unittest.TestCase):
    def test_execute_agentic_loop_propagates_exhaustion_metadata(self):
        bridge = AIBridge(config={})
        node = {"type": "builder", "key": "builder1"}
        model_info = {"litellm_id": "kimi-k2.5", "provider": "kimi"}

        async def _run():
            with patch("agentic_runtime.AgenticLoop.run", return_value={
                "success": False,
                "exhausted": True,
                "exhaustion_reason": "max_iterations",
                "output": "partial draft",
                "tool_results": [],
                "iterations": 20,
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                "tool_call_stats": {},
                "files_created": [],
                "files_modified": [],
                "context_compressions": {"snip": 1, "micro": 0, "full": 0},
            }):
                result = await bridge._execute_agentic_loop(node, [], "build a game", model_info)
                self.assertFalse(result["success"])
                self.assertTrue(result["exhausted"])
                self.assertEqual(result["exhaustion_reason"], "max_iterations")
                self.assertIn("max_iterations", result["error"])

        asyncio.run(_run())


class TestReportAndHandoffBuilders(unittest.TestCase):
    def setUp(self):
        self.sample_result = {
            "success": True,
            "output": "done",
            "files_created": ["assets/hero.md"],
            "files_modified": ["index.html"],
            "tool_call_stats": {"web_fetch": 1, "bash": 1},
            "tool_results": [
                {
                    "tool": "web_fetch",
                    "args": {"url": "https://example.com/tps-camera"},
                    "result": "Reference notes from https://example.com/tps-camera",
                    "error": None,
                    "started_at": 1.0,
                    "duration_ms": 12,
                },
                {
                    "tool": "bash",
                    "args": {"command": "npm test"},
                    "result": "ok",
                    "error": None,
                    "started_at": 2.0,
                    "duration_ms": 20,
                },
            ],
            "search_queries": ["tps camera"],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            "duration_seconds": 5.0,
            "iterations": 2,
            "context_compressions": {"snip": 1, "micro": 0, "full": 0},
        }

    def test_report_generator_extracts_reference_urls_from_web_fetch(self):
        report = ReportGenerator.from_agentic_result(
            self.sample_result,
            node_label="Analyst",
            node_type="analyst",
            node_key="analyst",
            model_used="kimi-coding",
            task_brief="research",
            lang="zh",
        )
        self.assertIn("https://example.com/tps-camera", report.reference_urls)
        self.assertEqual(len(report.tool_call_timeline), 2)

    def test_report_generator_renders_key_decision_entries_from_decision_field(self):
        sample = dict(self.sample_result)
        sample["traces"] = [{"summary": "Chose a shoulder-follow TPS camera to keep sightlines stable.", "tools": ["web_fetch"]}]
        report = ReportGenerator.from_agentic_result(
            sample,
            node_label="Builder",
            node_type="builder",
            node_key="builder1",
            model_used="kimi-coding",
            task_brief="build",
            lang="zh",
        )
        markdown = ReportGenerator.generate(report, lang="zh")
        self.assertIn("TPS camera", markdown)

    def test_handoff_builder_extracts_reference_urls_from_web_fetch(self):
        packet = HandoffBuilder.from_agentic_result(
            self.sample_result,
            source_node="analyst",
            source_node_type="analyst",
        )
        self.assertIn("https://example.com/tps-camera", packet.reference_urls)
        self.assertIn("Handoff Overview", packet.context_summary)
        self.assertIn("Downstream guidance", packet.context_summary)


class TestAIBridgeReportAndHandoffPostProcessing(unittest.TestCase):
    def test_execute_prefers_ui_language_and_structured_handoff_summary(self):
        bridge = AIBridge(config={"ui_language": "en"})
        rich_result = {
            "success": True,
            "output": "RAW OUTPUT SHOULD NOT BECOME THE ONLY HANDOFF SUMMARY",
            "files_created": ["index.html"],
            "files_modified": ["app.js"],
            "tool_call_stats": {"file_write": 1, "file_edit": 1},
            "tool_results": [
                {
                    "tool": "file_write",
                    "args": {"path": "index.html", "content": "<!doctype html><html><body>ok</body></html>"},
                    "result": "ok",
                    "error": None,
                    "started_at": 1.0,
                    "duration_ms": 10,
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14},
            "duration_seconds": 3.0,
            "iterations": 2,
        }

        async def _run():
            with patch.object(bridge, "resolve_node_model_candidates", return_value=["kimi-coding"]), \
                 patch.object(bridge, "_resolve_model", return_value={"provider": "kimi"}), \
                 patch.object(bridge, "_execute_single_model", new=AsyncMock(return_value=rich_result)):
                return await bridge.execute(
                    {"type": "builder", "key": "builder1", "label": "Builder 1"},
                    [],
                    "Build a patch-based TPS prototype",
                    model="kimi-coding",
                )

        result = asyncio.run(_run())
        self.assertIn("# Builder 1 — Execution Report", result["walkthrough_report"])
        # v3.5.1: _build_structured_summary now uses "completed all responsibilities" with bold markdown
        self.assertIn("completed all responsibilities", result["handoff_packet"]["context_summary"])
        self.assertIn("Stack:", result["handoff_packet"]["context_summary"])
        self.assertIn("## Deliverables", result["handoff_context_message"])
        self.assertNotEqual(result["handoff_packet"]["context_summary"], rich_result["output"][:2000])


class TestOrchestratorRollbackReport(unittest.TestCase):
    def test_reviewer_rollback_report_mentions_patch_mode(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        markdown = orch._build_reviewer_rollback_report_md(
            node_title="reviewer",
            reviewer_output='{"verdict":"REJECTED","issues":["camera inverted"]}',
            rejection_details="Controls are inverted and the crosshair is missing.",
            issues_found=["Mouse Y is inverted.", "Crosshair is missing."],
            required_changes=["Fix mouse inversion in the existing input handler.", "Add a visible centered crosshair without rewriting the whole HUD."],
            acceptance_criteria=["Mouse look directions must match drag direction.", "Crosshair stays visible during combat."],
        )
        self.assertIn("定点补丁", markdown)
        self.assertIn("回归复验标准", markdown)

    def test_human_file_inventory_lines_include_purpose_text(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        lines = orch._human_file_inventory_lines(
            ["/tmp/evermind_output/index.html", "/tmp/evermind_output/app.js"],
            role="builder",
            goal="创建一个第三人称射击游戏",
        )
        joined = "\n".join(lines)
        self.assertIn("主入口页面", joined)
        self.assertIn("运行时脚本", joined)


class TestFileEditSemantics(unittest.TestCase):
    def test_file_edit_rejects_whitespace_only_file_as_existing_content(self):
        async def _run():
            tmp_dir = pathlib.Path(tempfile.mkdtemp())
            target = tmp_dir / "notes.txt"
            target.write_text("   \n", encoding="utf-8")
            result = await tool_file_edit(str(target), "", "new content")
            self.assertFalse(result.success)
            self.assertIn("already exists with content", result.error)

        asyncio.run(_run())

    def test_tool_bash_honors_safe_cwd(self):
        async def _run():
            tmp_dir = pathlib.Path(tempfile.mkdtemp())
            result = await tool_bash("pwd", cwd=str(tmp_dir))
            self.assertTrue(result.success)
            resolved_tmp_dir = str(tmp_dir.resolve())
            self.assertIn(resolved_tmp_dir, result.output)
            self.assertEqual(result.metadata.get("cwd"), resolved_tmp_dir)

        asyncio.run(_run())


class TestSupportLaneBuilderQuality(unittest.TestCase):
    def test_support_lane_substantial_html_is_not_rejected_twice(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        builder1 = SubTask(id="1", agent_type="builder", description="build core shell", depends_on=[])
        builder2 = SubTask(
            id="2",
            agent_type="builder",
            description="build support lane\nBuild a non-overlapping support subsystem for the same commercial-grade HTML5 game.",
            depends_on=[],
        )
        plan = Plan(
            goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、关卡和第三人称视角。",
            subtasks=[builder1, builder2],
        )
        html = "<!DOCTYPE html><html><head><title>Support Lane</title></head><body>" + ("A" * 5000) + "</body></html>"

        with tempfile.TemporaryDirectory() as td:
            tmp_out = pathlib.Path(td)
            (tmp_out / "index.html").write_text(html, encoding="utf-8")
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                report = orch._validate_builder_quality([], html, goal=plan.goal, plan=plan, subtask=builder2)

        self.assertTrue(report["pass"])
        warnings = " | ".join(str(item) for item in report.get("warnings", []))
        self.assertIn("Support-lane builder produced HTML", warnings)


class TestWriteProgressMetrics(unittest.TestCase):
    def test_written_file_code_metrics_are_extracted_from_saved_artifact(self):
        bridge = AIBridge(config={})
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / "support.js"
            target.write_text("const score = 0;\nfunction fire() {\n  return score + 1;\n}\n", encoding="utf-8")
            metrics = bridge._written_file_code_metrics(str(target))

        self.assertEqual(metrics["code_lines"], 4)
        self.assertEqual(metrics["total_lines"], 4)
        self.assertGreater(metrics["code_kb"], 0)
        self.assertIn("JS", metrics["languages"])

    def test_aggregate_code_metrics_dedupes_staged_root_copy(self):
        bridge = AIBridge(config={})
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        with tempfile.TemporaryDirectory() as td:
            output_dir = pathlib.Path(td)
            staged_dir = output_dir / "task_5"
            staged_dir.mkdir(parents=True, exist_ok=True)
            html = "<!DOCTYPE html><html><head><title>Demo</title></head><body><script>const hero = 1;\nfunction fire(){ return hero + 1; }</script></body></html>\n"
            js = "export const hud = true;\nexport function mountHud(){ return hud; }\n"
            (output_dir / "index.html").write_text(html, encoding="utf-8")
            (staged_dir / "index.html").write_text(html, encoding="utf-8")
            (output_dir / "app.js").write_text(js, encoding="utf-8")

            with patch.object(orchestrator_module, "OUTPUT_DIR", output_dir):
                metrics = orch._aggregate_code_metrics_for_files([
                    str(output_dir / "index.html"),
                    str(staged_dir / "index.html"),
                    str(output_dir / "app.js"),
                ])

        expected_total_lines = bridge._count_code_metrics(html)["total_lines"] + bridge._count_code_metrics(js)["total_lines"]
        self.assertGreater(metrics["code_lines"], 0)
        self.assertEqual(metrics["total_lines"], expected_total_lines)
        self.assertIn("HTML", metrics["languages"])
        self.assertIn("JS", metrics["languages"])


class TestParallelBuilderStaging(unittest.TestCase):
    def test_primary_builder_with_merger_stages_root_artifact_in_task_dir(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        goal = "创建一个第三人称 3D 射击游戏，要有怪物、枪械、关卡和第三人称视角。"
        builder1 = SubTask(id="5", agent_type="builder", description="builder 1 core shell", depends_on=[])
        builder2 = SubTask(id="6", agent_type="builder", description="builder 2 support lane", depends_on=[])
        merger = SubTask(id="7", agent_type="builder", description=orch._merger_task_description(goal), depends_on=["5", "6"])
        plan = Plan(goal=goal, subtasks=[builder1, builder2, merger])

        self.assertTrue(orch._builder_requires_staged_root_artifact(plan, builder1, goal))
        self.assertFalse(orch._builder_live_root_publish_enabled(plan, builder1, goal))

        with tempfile.TemporaryDirectory() as td:
            output_dir = pathlib.Path(td)
            staged_dir = output_dir / "task_5"
            blob = """```html index.html
<!DOCTYPE html>
<html>
<head><title>Staged Build</title></head>
<body><canvas id="game"></canvas><script>const ready = true;</script></body>
</html>
```"""
            with patch.object(orchestrator_module, "OUTPUT_DIR", output_dir):
                files = orch._extract_and_save_code(
                    blob,
                    builder1.id,
                    allow_root_index_copy=False,
                    allow_root_shared_asset_write=False,
                    allowed_html_targets=["index.html"],
                    output_root=staged_dir,
                )

            self.assertIn(str(staged_dir / "index.html"), files)
            self.assertFalse((output_dir / "index.html").exists())


class TestPlannerFallbackTokenAccounting(unittest.TestCase):
    def test_planner_fallback_emits_estimated_tokens(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._persist_execution_observability_artifacts = lambda *args, **kwargs: None
        orch._append_ne_activity = lambda *args, **kwargs: None
        orch._humanize_output_summary = lambda *args, **kwargs: "planner summary"
        orch._summarize_node_work = lambda *args, **kwargs: ["fallback summary"]
        subtask = SubTask(id="1", agent_type="planner", description="规划一个第三人称射击游戏的执行方案", depends_on=[])
        plan = Plan(goal="创建一个第三人称 3D 射击游戏", subtasks=[subtask])

        async def _run():
            await orch._finalize_planner_fallback(
                subtask,
                plan,
                "## Planner Fallback\n- 保留并行 builder 与 merger。",
                mode="planner_timeout_fallback",
                note="Planner 超时，已切换到 deterministic blueprint fallback。",
                prev_results={},
            )

        asyncio.run(_run())
        sync_kwargs = orch._sync_ne_status.await_args.kwargs
        self.assertGreater(sync_kwargs.get("tokens_used", 0), 0)
        payload = orch.emit.await_args_list[-1].args[1]
        self.assertGreater(payload["tokens_used"], 0)
        self.assertGreater(payload["prompt_tokens"], 0)
        self.assertGreater(payload["completion_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
