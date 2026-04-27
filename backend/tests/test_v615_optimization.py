"""v6.1.5 optimization regression tests — ThinHandoff + EdgeProcessor + DirectText."""
from __future__ import annotations

import unittest

from task_handoff import (
    HandoffPacket,
    BUILT_IN_PROCESSORS,
    apply_edge_processor,
    drop_verbose_fields,
    keep_file_refs_only,
    summarize_brief,
)


class TestThinHandoffRender(unittest.TestCase):
    def test_thin_render_preserves_blockers(self):
        p = HandoffPacket(
            source_node_type="analyst",
            context_summary="研究完成 — 发现关键约束",
            warnings=["IMPORTANT: missing Godogen TPS anchor"],
        )
        out = p.to_context_message(lang="zh", verbose=False)
        self.assertIn("IMPORTANT", out)
        self.assertLess(len(out), 1200)

    def test_thin_render_preserves_open_question(self):
        p = HandoffPacket(
            source_node_type="analyst",
            context_summary="brief",
            open_questions=["Should we use Three.js or plain WebGL?"],
        )
        out = p.to_context_message(lang="zh", verbose=False)
        self.assertTrue("Three.js" in out or "疑问" in out)

    def test_verbose_render_includes_design_choices(self):
        p = HandoffPacket(
            source_node_type="builder",
            context_summary="built",
            design_choices=[{"choice": "Three.js over Babylon", "rationale": "smaller bundle"}],
        )
        thin = p.to_context_message(lang="en", verbose=False)
        verbose = p.to_context_message(lang="en", verbose=True)
        self.assertNotIn("Babylon", thin)
        self.assertIn("Babylon", verbose)


class TestEdgePayloadProcessors(unittest.TestCase):
    def test_drop_verbose_strips_heavy_fields(self):
        p = HandoffPacket(
            source_node="analyst",
            context_summary="summary",
            source_bundles=[{"title": "t", "url": "u", "content_summary": "x" * 1000}],
            reference_urls=["u1", "u2"],
            technologies_used=["Three.js"],
            token_usage={"prompt_tokens": 1000},
        )
        out = drop_verbose_fields(p)
        self.assertEqual(out.source_bundles, [])
        self.assertEqual(out.reference_urls, [])
        self.assertEqual(out.technologies_used, [])
        self.assertEqual(out.token_usage, {})
        self.assertEqual(out.context_summary, "summary")  # preserved

    def test_file_refs_only_keeps_paths(self):
        p = HandoffPacket(
            source_node="builder",
            source_node_type="builder",
            files_produced=[{"path": "/tmp/x.html", "purpose": "main"}],
            decisions_made=["used Three.js"],
            design_choices=[{"choice": "A", "rationale": "B"}],
        )
        out = keep_file_refs_only(p)
        self.assertEqual(out.files_produced, p.files_produced)
        self.assertEqual(out.decisions_made, [])
        self.assertEqual(out.design_choices, [])

    def test_summarize_brief_caps_length(self):
        p = HandoffPacket(context_summary="x" * 500)
        proc = summarize_brief(200)
        out = proc(p)
        self.assertLessEqual(len(out.context_summary), 200)
        self.assertTrue(out.context_summary.endswith("..."))

    def test_builtin_processors_registered(self):
        for name in ("drop_verbose", "file_refs_only", "summarize_200", "summarize_400"):
            self.assertIn(name, BUILT_IN_PROCESSORS)

    def test_apply_by_name(self):
        p = HandoffPacket(source_bundles=[{"title": "x"}])
        out = apply_edge_processor(p, "drop_verbose")
        self.assertEqual(out.source_bundles, [])

    def test_apply_unknown_name_passes_through(self):
        p = HandoffPacket(context_summary="unchanged")
        out = apply_edge_processor(p, "not_a_real_processor")
        self.assertEqual(out.context_summary, "unchanged")

    def test_apply_none_processor_returns_packet(self):
        p = HandoffPacket(context_summary="x")
        self.assertIs(apply_edge_processor(p, None), p)


class TestOrchestratorEdgeDispatch(unittest.TestCase):
    def setUp(self):
        from orchestrator import Orchestrator
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_tester_gets_summarize_200(self):
        self.assertEqual(
            self.orch._pick_edge_payload_processor("builder", "tester"),
            "summarize_200",
        )

    def test_deployer_gets_file_refs_only(self):
        self.assertEqual(
            self.orch._pick_edge_payload_processor("builder", "deployer"),
            "file_refs_only",
        )

    def test_analyst_to_reviewer_drops_verbose(self):
        self.assertEqual(
            self.orch._pick_edge_payload_processor("analyst", "reviewer"),
            "drop_verbose",
        )

    def test_analyst_to_builder_is_passthrough(self):
        # v6.1.5 (Opus R1): must NOT slim — legacy analyst_handoff path needs
        # to run to extract <reference_code_snippets>
        self.assertIsNone(
            self.orch._pick_edge_payload_processor("analyst", "builder"),
        )

    def test_reviewer_to_debugger_is_passthrough(self):
        self.assertIsNone(
            self.orch._pick_edge_payload_processor("reviewer", "debugger"),
        )


class TestPlannerBriefGate(unittest.TestCase):
    def setUp(self):
        from orchestrator import Orchestrator
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def _make_subtask(self, description: str):
        class _T:
            pass
        t = _T()
        t.description = description
        return t

    def test_specific_brief_with_multiple_markers_fires(self):
        desc = ("Build: camera system, player controller, WASD mapping, "
                "collision detection. Tech: Three.js. "
                "Output: /tmp/evermind_output/task_N/index.html. "
                "Deliver a complete vertical slice.")
        self.assertTrue(
            self.orch._planner_task_specific_enough_for_direct_text(
                self._make_subtask(desc)
            )
        )

    def test_short_brief_rejected(self):
        self.assertFalse(
            self.orch._planner_task_specific_enough_for_direct_text(
                self._make_subtask("Build something cool")
            )
        )

    def test_long_but_no_markers_rejected(self):
        self.assertFalse(
            self.orch._planner_task_specific_enough_for_direct_text(
                self._make_subtask("a" * 300)
            )
        )


if __name__ == "__main__":
    unittest.main()


class TestCondensation(unittest.TestCase):
    """v6.1.6 — agentic_runtime._try_condensation regression tests."""

    def _make_loop(self):
        from agentic_runtime import AgenticLoop, LoopConfig, ContextWindow

        class _FakeTools:
            def get_tool_definitions(self, *a, **k):
                return []
            def get(self, name):
                return None

        async def _fake_llm(messages, tools=None, tool_choice=None):
            return {"message": {"content": "", "tool_calls": None}, "usage": {}}

        config = LoopConfig(
            node_type="builder", node_key="builder1",
            max_iterations=3, max_tool_calls=3,
        )
        loop = AgenticLoop(config, _FakeTools(), _fake_llm)
        return loop

    def test_condensation_requires_enough_messages(self):
        loop = self._make_loop()
        # fewer than 10 messages → no condensation
        for i in range(5):
            loop.context.add_message("assistant", f"msg {i}")
        self.assertFalse(loop._try_condensation("test"))

    def test_condensation_shrinks_old_tool_results(self):
        loop = self._make_loop()
        loop.context.add_message("system", "sys")
        loop.context.add_message("user", "task")
        big = "X" * 2000
        for i in range(8):
            loop.context.add_tool_result(f"tc_{i}", big)
        ok = loop._try_condensation("loop_detected")
        self.assertTrue(ok, "should condense with 8 fat tool results")
        # last 4 tool_results should be untouched
        tool_msgs = [m for m in loop.context.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs[-1]["content"]), 2000)
        # earlier ones shrunk
        self.assertLess(len(tool_msgs[0]["content"]), 1000)
        self.assertIn("condensed", tool_msgs[0]["content"])

    def test_condensation_honors_max_attempts(self):
        loop = self._make_loop()
        loop.context.add_message("system", "sys")
        for i in range(10):
            loop.context.add_tool_result(f"tc_{i}", "Y" * 1500)
        self.assertTrue(loop._try_condensation("r1"))
        # Second attempt should fail (1 rescue per run)
        self.assertFalse(loop._try_condensation("r2"))


class TestUnifiedDiff(unittest.TestCase):
    def test_simple_hunk_apply(self):
        from plugins.implementations import _apply_unified_diff
        orig = "line1\nline2\nline3\nline4\n"
        patch = (
            "--- a/f\n+++ b/f\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n-line2\n+LINE_TWO\n line3\n"
        )
        result = _apply_unified_diff(orig, patch)
        self.assertIn("LINE_TWO", result)
        self.assertNotIn("line2\n", result.replace("LINE_TWO", ""))

    def test_raises_when_hunk_not_found(self):
        from plugins.implementations import _apply_unified_diff, _PatchApplyError
        orig = "line1\nline2\n"
        patch = (
            "--- a/f\n+++ b/f\n"
            "@@ -1,2 +1,2 @@\n"
            " notThere\n-wrongLine\n+fixed\n"
        )
        with self.assertRaises(_PatchApplyError):
            _apply_unified_diff(orig, patch)

    def test_multi_hunk_preserves_middle_content(self):
        """Opus R2 regression: old impl duplicated prefix on 2nd hunk."""
        from plugins.implementations import _apply_unified_diff
        orig = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\n"
        patch = (
            "--- a/f\n+++ b/f\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n-line2\n+LINE2\n line3\n"
            "@@ -6,3 +6,3 @@\n"
            " line6\n-line7\n+LINE7\n line8\n"
        )
        result = _apply_unified_diff(orig, patch)
        expected = "line1\nLINE2\nline3\nline4\nline5\nline6\nLINE7\nline8\n"
        self.assertEqual(result, expected)

    def test_new_file_via_dev_null(self):
        """Aider-style /dev/null patch creates a fresh file from scratch."""
        from plugins.implementations import _apply_unified_diff
        patch = (
            "--- /dev/null\n+++ b/new.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+print(1)\n+print(2)\n+print(3)\n"
        )
        result = _apply_unified_diff("", patch)
        self.assertEqual(result, "print(1)\nprint(2)\nprint(3)\n")


class TestToolsCacheBreakpoint(unittest.TestCase):
    def test_anthropic_model_stamps_last_tool(self):
        from ai_bridge import AIBridge
        b = AIBridge(config={})
        tools = [
            {"type": "function", "function": {"name": "a", "description": "A"}},
            {"type": "function", "function": {"name": "b", "description": "B"}},
        ]
        out = b._apply_tools_cache_breakpoint(tools, model="claude-opus-4-7")
        self.assertIsNotNone(out)
        self.assertIn("cache_control", out[-1]["function"])
        self.assertNotIn("cache_control", out[0]["function"])

    def test_non_anthropic_passthrough(self):
        from ai_bridge import AIBridge
        b = AIBridge(config={})
        tools = [{"type": "function", "function": {"name": "a"}}]
        out = b._apply_tools_cache_breakpoint(tools, model="kimi-coding")
        self.assertIs(out, tools)  # identity — no copy needed

    def test_none_input(self):
        from ai_bridge import AIBridge
        b = AIBridge(config={})
        self.assertIsNone(b._apply_tools_cache_breakpoint(None, model="claude-opus-4-7"))


class TestSkillFrontmatter(unittest.TestCase):
    def test_frontmatter_triggers_match_goal(self):
        from agent_skills import _resolve_frontmatter_skills
        hits = _resolve_frontmatter_skills("builder", "build a 3D FPS shooter with WASD controls")
        self.assertIn("godogen-tps-control-sanity-lock", hits)

    def test_frontmatter_rejects_unmatched(self):
        from agent_skills import _resolve_frontmatter_skills
        hits = _resolve_frontmatter_skills("builder", "write a static landing page")
        self.assertNotIn("godogen-tps-control-sanity-lock", hits)

    def test_frontmatter_respects_node_types(self):
        from agent_skills import _resolve_frontmatter_skills
        hits = _resolve_frontmatter_skills("imagegen", "3D shooter with WASD")
        # imagegen not in godogen-tps activation_node_types
        self.assertNotIn("godogen-tps-control-sanity-lock", hits)


class TestGitHubPluginShape(unittest.TestCase):
    def test_plugin_registered(self):
        from plugins.base import PluginRegistry
        from plugins.implementations import register_all, GitHubPlugin
        # Test ordering may have cleared the registry; register directly if
        # needed so this assertion is deterministic regardless of run order.
        try:
            existing = PluginRegistry.get_all()
        except Exception:
            existing = {}
        if "github" not in existing:
            try:
                PluginRegistry.register(GitHubPlugin())
            except Exception:
                register_all()
        self.assertIn("github", PluginRegistry.get_all())

    def test_schema_has_required_actions(self):
        from plugins.implementations import GitHubPlugin
        schema = GitHubPlugin()._get_parameters_schema()
        actions = set(schema["properties"]["action"]["enum"])
        self.assertTrue({"connect", "status", "list_repos", "create_repo", "push"}.issubset(actions))


class TestWave_A_IncrementalStream(unittest.TestCase):
    def test_boundary_flush_on_close_style(self):
        from incremental_stream_parser import IncrementalStreamParser
        flushes = []
        p = IncrementalStreamParser(
            target_path="/tmp/_test_noop.html",
            on_flush=lambda path, c, final: flushes.append((len(c), final)),
        )
        p.feed("<!DOCTYPE html><html><head><style>" + "body{}" * 100)
        p.feed("</style>")
        # more content + </html> forces final flush
        p.feed("</head><body>stuff</body></html>")
        p.finalize()
        self.assertGreaterEqual(len(flushes), 2)
        # at least one non-final boundary flush
        self.assertTrue(any(not f[1] for f in flushes))
        # last flush always marks final
        self.assertTrue(flushes[-1][1])

    def test_no_boundary_no_flush_below_cap(self):
        from incremental_stream_parser import IncrementalStreamParser
        flushes = []
        p = IncrementalStreamParser(
            target_path="/tmp/_test2.html", max_buffer_bytes=100_000,
            on_flush=lambda path, c, f: flushes.append((len(c), f)),
        )
        p.feed("just plain text with no html tags at all")
        self.assertEqual(len(flushes), 0)  # no flush until finalize
        p.finalize()
        self.assertEqual(len(flushes), 1)
        self.assertTrue(flushes[0][1])

    def test_reset_flushes_old_and_switches(self):
        from incremental_stream_parser import IncrementalStreamParser
        flushes = []
        p = IncrementalStreamParser(
            target_path="/tmp/_a.html",
            on_flush=lambda path, c, f: flushes.append((path, len(c), f)),
        )
        p.feed("<html><body>A</body></html>")
        p.reset("/tmp/_b.html")
        p.feed("<html><body>B</body></html>")
        p.finalize()
        # should have flushes for both paths
        paths = [f[0] for f in flushes]
        self.assertTrue(any("/_a.html" in pp for pp in paths))
        self.assertTrue(any("/_b.html" in pp for pp in paths))


class TestWave_B_PrefillSupport(unittest.TestCase):
    def test_supports_anthropic(self):
        from ai_bridge import AIBridge
        b = AIBridge(config={})
        self.assertTrue(b._provider_supports_assistant_prefill(
            {"provider": "anthropic", "litellm_id": "anthropic/claude-opus-4-7"}
        ))

    def test_supports_deepseek(self):
        from ai_bridge import AIBridge
        b = AIBridge(config={})
        self.assertTrue(b._provider_supports_assistant_prefill(
            {"provider": "deepseek", "litellm_id": "deepseek/chat"}
        ))

    def test_supports_moonshot_direct(self):
        from ai_bridge import AIBridge
        b = AIBridge(config={})
        self.assertTrue(b._provider_supports_assistant_prefill(
            {"provider": "moonshot", "litellm_id": "moonshot/kimi-k2"}
        ))

    def test_rejects_moonshot_relay(self):
        from ai_bridge import AIBridge
        b = AIBridge(config={})
        self.assertFalse(b._provider_supports_assistant_prefill(
            {"provider": "kimi", "litellm_id": "relay/aigate-kimi-k2"}
        ))

    def test_rejects_openai_gpt(self):
        from ai_bridge import AIBridge
        b = AIBridge(config={})
        self.assertFalse(b._provider_supports_assistant_prefill(
            {"provider": "openai", "litellm_id": "openai/gpt-5.4"}
        ))
