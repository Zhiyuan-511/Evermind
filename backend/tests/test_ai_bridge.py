"""
Evermind Backend — AI Bridge Unit Tests
Covers model resolution, usage normalization, and cost estimation.
"""

import unittest
from unittest.mock import MagicMock, patch
import asyncio

from ai_bridge import (
    AIBridge,
    MODEL_REGISTRY,
    MAX_REQUEST_TOTAL_CHARS,
    _sanitize_error,
)


class TestResolveModel(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_resolves_static_registry_model(self):
        info = self.bridge._resolve_model("gpt-4o")
        self.assertEqual(info["litellm_id"], "gpt-4o")
        self.assertEqual(info["provider"], "openai")
        self.assertTrue(info["supports_tools"])

    def test_resolves_deepseek_model(self):
        info = self.bridge._resolve_model("deepseek-v3")
        self.assertEqual(info["litellm_id"], "deepseek/deepseek-chat")
        self.assertEqual(info["provider"], "deepseek")

    def test_fallback_for_unknown_model(self):
        info = self.bridge._resolve_model("some-future-model-v99")
        self.assertEqual(info["litellm_id"], "some-future-model-v99")
        self.assertTrue(info["supports_tools"])
        self.assertFalse(info["supports_cua"])

    def test_kimi_model_has_api_base(self):
        info = self.bridge._resolve_model("kimi")
        self.assertIn("api_base", info)
        self.assertTrue("kimi" in info["api_base"] or "moonshot" in info["api_base"])

    def test_qwen_model_has_api_base(self):
        info = self.bridge._resolve_model("qwen-max")
        self.assertIn("api_base", info)
        self.assertIn("dashscope", info["api_base"])


class TestNormalizeUsage(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_none_usage_returns_zeros(self):
        result = self.bridge._normalize_usage(None)
        self.assertEqual(result["prompt_tokens"], 0)
        self.assertEqual(result["completion_tokens"], 0)
        self.assertEqual(result["total_tokens"], 0)

    def test_dict_usage(self):
        result = self.bridge._normalize_usage({
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        })
        self.assertEqual(result["prompt_tokens"], 100)
        self.assertEqual(result["completion_tokens"], 50)
        self.assertEqual(result["total_tokens"], 150)

    def test_input_output_tokens_fallback(self):
        """Anthropic-style usage keys should be handled."""
        result = self.bridge._normalize_usage({
            "input_tokens": 200,
            "output_tokens": 80,
        })
        self.assertEqual(result["prompt_tokens"], 200)
        self.assertEqual(result["completion_tokens"], 80)
        self.assertEqual(result["total_tokens"], 280)

    def test_model_dump_object(self):
        """Objects with model_dump() (Pydantic v2) should be handled."""
        mock = MagicMock()
        mock.model_dump.return_value = {
            "prompt_tokens": 50,
            "completion_tokens": 25,
            "total_tokens": 75,
        }
        result = self.bridge._normalize_usage(mock)
        self.assertEqual(result["prompt_tokens"], 50)
        self.assertEqual(result["total_tokens"], 75)


class TestMergeUsage(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_merges_two_dicts(self):
        base = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        delta = {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}
        result = self.bridge._merge_usage(base, delta)
        self.assertEqual(result["prompt_tokens"], 300)
        self.assertEqual(result["completion_tokens"], 150)
        self.assertEqual(result["total_tokens"], 450)

    def test_merge_with_none_delta(self):
        base = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        result = self.bridge._merge_usage(base, None)
        self.assertEqual(result, base)


class TestGetAvailableModels(unittest.TestCase):
    def test_includes_static_registry(self):
        bridge = AIBridge(config={})
        models = bridge.get_available_models()
        model_ids = {m["id"] for m in models}
        self.assertIn("gpt-4o", model_ids)
        self.assertIn("deepseek-v3", model_ids)
        self.assertIn("kimi", model_ids)

    def test_model_count_matches_registry(self):
        bridge = AIBridge(config={})
        models = bridge.get_available_models()
        # Should have at least as many as the static registry
        self.assertGreaterEqual(len(models), len(MODEL_REGISTRY))


class TestNodeTokenAndTimeoutPolicy(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_builder_defaults_are_higher(self):
        self.assertEqual(self.bridge._max_tokens_for_node("builder"), 8192)
        self.assertEqual(self.bridge._timeout_for_node("builder"), 180)

    def test_non_builder_defaults_are_lower(self):
        self.assertEqual(self.bridge._max_tokens_for_node("tester"), 4096)
        self.assertEqual(self.bridge._timeout_for_node("tester"), 90)
        self.assertEqual(self.bridge._max_tool_iterations_for_node("tester"), 3)

    def test_env_overrides_are_clamped(self):
        with patch.dict("os.environ", {
            "EVERMIND_BUILDER_MAX_TOKENS": "999999",
            "EVERMIND_BUILDER_TIMEOUT_SEC": "5",
            "EVERMIND_MAX_TOKENS": "-1",
            "EVERMIND_TIMEOUT_SEC": "999",
            "EVERMIND_BUILDER_MAX_TOOL_ITERS": "100",
            "EVERMIND_DEFAULT_MAX_TOOL_ITERS": "0",
        }):
            self.assertEqual(self.bridge._max_tokens_for_node("builder"), 16384)
            self.assertEqual(self.bridge._timeout_for_node("builder"), 30)
            self.assertEqual(self.bridge._max_tokens_for_node("tester"), 1024)
            self.assertEqual(self.bridge._timeout_for_node("tester"), 600)
            self.assertEqual(self.bridge._max_tool_iterations_for_node("builder"), 20)
            self.assertEqual(self.bridge._max_tool_iterations_for_node("tester"), 1)

    def test_analyst_browser_limit_defaults_to_two(self):
        self.assertEqual(self.bridge._analyst_browser_call_limit(), 2)
        self.assertFalse(self.bridge._should_block_browser_call("analyst", {"browser": 1}))
        self.assertTrue(self.bridge._should_block_browser_call("analyst", {"browser": 2}))
        self.assertFalse(self.bridge._should_block_browser_call("builder", {"browser": 99}))

    def test_analyst_browser_limit_can_be_overridden(self):
        with patch.dict("os.environ", {"EVERMIND_ANALYST_MAX_BROWSER_CALLS": "1"}):
            self.assertEqual(self.bridge._analyst_browser_call_limit(), 1)
            self.assertTrue(self.bridge._should_block_browser_call("analyst", {"browser": 1}))

    def test_stream_stall_timeout_defaults_are_role_aware(self):
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("builder"), 300)
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("tester"), 180)

    def test_stream_stall_timeout_env_overrides_are_clamped(self):
        with patch.dict("os.environ", {
            "EVERMIND_BUILDER_STREAM_STALL_SEC": "9999",
            "EVERMIND_STREAM_STALL_SEC": "5",
        }):
            self.assertEqual(self.bridge._stream_stall_timeout_for_node("builder"), 600)
            self.assertEqual(self.bridge._stream_stall_timeout_for_node("tester"), 30)


class TestBuilderForcedOutputPolicy(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_force_when_builder_has_no_html_and_no_file_write(self):
        self.assertTrue(self.bridge._builder_needs_forced_text("builder", "I will now create a page.", []))

    def test_no_force_when_builder_already_has_html(self):
        self.assertFalse(self.bridge._builder_needs_forced_text("builder", "<!DOCTYPE html><html><head></head><body></body></html>", []))

    def test_no_force_when_builder_already_wrote_file(self):
        tool_results = [{"success": True, "data": {"path": "/tmp/evermind_output/index.html", "written": True}}]
        self.assertFalse(self.bridge._builder_needs_forced_text("builder", "Done", tool_results))

    def test_no_force_for_non_builder_nodes(self):
        self.assertFalse(self.bridge._builder_needs_forced_text("tester", "no html", []))

    def test_builder_system_prompt_is_task_adaptive_for_game(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "builder"},
            plugins=[],
            input_data="请帮我做一个贪吃蛇小游戏",
        )
        self.assertIn("GAME DESIGN SYSTEM", prompt)

    def test_infer_file_ops_action_prefers_explicit_args(self):
        action = self.bridge._infer_file_ops_action(
            "{\"action\":\"write\",\"path\":\"/tmp/evermind_output/index.html\"}",
            {"success": True, "data": {"written": True}},
        )
        self.assertEqual(action, "write")

    def test_infer_file_ops_action_falls_back_to_result_shape(self):
        action = self.bridge._infer_file_ops_action(
            "{bad json",
            {"success": True, "data": {"path": "/tmp/evermind_output", "entries": []}},
        )
        self.assertEqual(action, "list")


class TestAssistantSerialization(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_keeps_reasoning_content_in_serialized_message(self):
        class Fn:
            name = "file_ops"
            arguments = "{\"action\":\"list\"}"

        class ToolCall:
            id = "call_1"
            type = "function"
            function = Fn()

        class Msg:
            content = ""
            tool_calls = [ToolCall()]
            reasoning_content = "thinking trace"

            def model_dump(self, exclude_none=True):
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_ops", "arguments": "{\"action\":\"list\"}"},
                    }],
                    "reasoning_content": "thinking trace",
                }

        payload = self.bridge._serialize_assistant_message(Msg())
        self.assertEqual(payload.get("role"), "assistant")
        self.assertIn("reasoning_content", payload)
        self.assertEqual(payload["reasoning_content"], "thinking trace")
        self.assertEqual(payload["tool_calls"][0]["function"]["name"], "file_ops")


class TestRunPluginArgNormalization(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_none_args_become_empty_dict(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "stub"

            async def execute(self, params, context=None):
                return ResultObj() if params == {} else None

        out = asyncio.run(self.bridge._run_plugin("stub", None, [StubPlugin()]))
        self.assertEqual(out.get("ok"), True)

    def test_malformed_json_args_do_not_crash(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "stub"

            async def execute(self, params, context=None):
                return ResultObj() if isinstance(params, dict) else None

        out = asyncio.run(self.bridge._run_plugin("stub", "{bad json", [StubPlugin()]))
        self.assertEqual(out.get("ok"), True)

    def test_plugin_not_in_allowlist_is_rejected(self):
        out = asyncio.run(self.bridge._run_plugin("browser", {"action": "navigate"}, []))
        self.assertIn("not enabled", (out.get("error") or "").lower())

    def test_reviewer_browser_forces_headful_context(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "browser"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj()

        self.bridge.config = {"reviewer_tester_force_headful": True, "browser_headful": False}
        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("browser", {"action": "navigate"}, [plugin], node_type="reviewer"))
        self.assertEqual(out.get("ok"), True)
        self.assertTrue(plugin.last_context.get("browser_headful"))
        self.assertEqual(plugin.last_context.get("browser_force_reason"), "reviewer_visible_review")

    def test_builder_browser_does_not_force_headful_context(self):
        class ResultObj:
            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "browser"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj()

        self.bridge.config = {"reviewer_tester_force_headful": True, "browser_headful": False}
        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("browser", {"action": "navigate"}, [plugin], node_type="builder"))
        self.assertEqual(out.get("ok"), True)
        self.assertFalse(plugin.last_context.get("browser_headful", False))


class TestContextCompaction(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_prepare_messages_caps_total_context_size(self):
        messages = [
            {"role": "system", "content": "S" * 24000},
            {"role": "user", "content": "U" * 24000},
        ]
        for i in range(18):
            messages.append({
                "role": "assistant",
                "content": "A" * 12000,
                "tool_calls": [{
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "file_ops", "arguments": "X" * 9000},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": "T" * 22000,
            })

        prepared = self.bridge._prepare_messages_for_request(messages, "kimi-coding")
        total_chars = sum(self.bridge._message_char_count(m) for m in prepared)
        self.assertLessEqual(total_chars, MAX_REQUEST_TOTAL_CHARS)
        self.assertTrue(any("OLDER_CONTEXT_OMITTED" in str(m.get("content", "")) for m in prepared))

    def test_sanitize_error_has_non_empty_fallback(self):
        self.assertEqual(_sanitize_error(""), "Unknown error")
        self.assertEqual(_sanitize_error(None), "Unknown error")


if __name__ == "__main__":
    unittest.main()
