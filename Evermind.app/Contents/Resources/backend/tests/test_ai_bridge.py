"""
Evermind Backend — AI Bridge Unit Tests
Covers model resolution, usage normalization, and cost estimation.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from ai_bridge import (
    AIBridge,
    AGENT_PRESETS,
    MODEL_REGISTRY,
    MAX_REQUEST_TOTAL_CHARS,
    _sanitize_error,
)
from plugins.base import PluginResult
from plugins.implementations import FileOpsPlugin


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


class TestNodeModelPreferences(unittest.TestCase):
    def test_resolve_candidates_prioritize_node_chain_over_default_model(self):
        bridge = AIBridge(config={
            "node_model_preferences": {
                "builder": ["claude-4-sonnet", "kimi-coding"],
            },
        })

        candidates = bridge.resolve_node_model_candidates(
            {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
            "gpt-5.4",
        )

        self.assertEqual(candidates[:3], ["claude-4-sonnet", "kimi-coding", "gpt-5.4"])

    def test_preferred_model_skips_missing_keys_inside_chain(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "KIMI_API_KEY": ""}):
            bridge = AIBridge(config={
                "kimi_api_key": "sk-kimi-test",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "claude-4-sonnet", "kimi-coding"],
                },
            })

            preferred = bridge.preferred_model_for_node(
                {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                "gpt-5.4",
            )

            self.assertEqual(preferred, "kimi-coding")

    def test_resolve_candidates_filters_unavailable_legacy_models(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "QWEN_API_KEY": "",
                "KIMI_API_KEY": "",
            },
        ):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
            })

            candidates = bridge.resolve_node_model_candidates(
                {"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                "gpt-5.4",
            )

            self.assertEqual(candidates[:3], ["gpt-5.4", "kimi-coding", "gpt-4o"])
            self.assertNotIn("claude-4-sonnet", candidates)
            self.assertNotIn("deepseek-v3", candidates)
            self.assertNotIn("gemini-2.5-pro", candidates)
            self.assertNotIn("qwen-max", candidates)

    def test_execute_falls_back_to_next_configured_model(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "anthropic_api_key": "sk-ant-test",
            "node_model_preferences": {
                "reviewer": ["claude-4-sonnet", "gpt-4o"],
            },
        })
        bridge._litellm = object()
        bridge._execute_litellm_chat = AsyncMock(side_effect=[
            {"success": False, "output": "", "error": "401 unauthorized"},
            {"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"},
        ])

        result = asyncio.run(
            bridge.execute(
                node={"type": "reviewer", "model": "gpt-5.4", "model_is_default": True},
                plugins=[],
                input_data="Review the generated site.",
                model="gpt-5.4",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("model"), "gpt-4o")
        self.assertEqual(result.get("attempted_models"), ["claude-4-sonnet", "gpt-4o"])
        self.assertTrue(result.get("model_chain_applied"))
        self.assertEqual(bridge._execute_litellm_chat.await_count, 2)

    def test_execute_falls_back_without_retrying_same_model_after_invalid_html_gateway_response(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "anthropic_api_key": "sk-ant-test",
            "node_model_preferences": {
                "reviewer": ["gpt-5.4", "claude-4-sonnet"],
            },
        })
        bridge._litellm = object()
        bridge._execute_litellm_chat = AsyncMock(side_effect=[
            {
                "success": False,
                "output": "",
                "error": (
                    "litellm.InternalServerError: OpenAIException - "
                    "Empty or invalid response from LLM endpoint. "
                    "Received: '<!doctype html><title>Relay - AI API Gateway</title>'"
                ),
            },
            {"success": True, "output": "ok", "tool_results": [], "mode": "litellm_chat"},
        ])

        result = asyncio.run(
            bridge.execute(
                node={"type": "reviewer", "model": "gpt-5.4", "model_is_default": True},
                plugins=[],
                input_data="Review the generated site.",
                model="gpt-5.4",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("model"), "claude-4-sonnet")

    def test_execute_preflight_progress_timeout_does_not_block_builder_dispatch(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )

        async def slow_progress(_payload):
            await asyncio.sleep(0.5)

        start = time.perf_counter()
        with patch.dict("os.environ", {"EVERMIND_PROGRESS_EVENT_TIMEOUT_SEC": "0.01"}):
            result = asyncio.run(
                bridge.execute(
                    node={
                        "type": "builder",
                        "model": "kimi-coding",
                        "model_is_default": False,
                        "builder_delivery_mode": "direct_multifile",
                    },
                    plugins=[],
                    input_data=(
                        "做一个 3 页面网站。\n"
                        "Assigned HTML filenames for this builder: index.html, pricing.html, contact.html."
                    ),
                    model="kimi-coding",
                    on_progress=slow_progress,
                )
            )
        elapsed = time.perf_counter() - start

        self.assertTrue(result.get("success"))
        self.assertLess(elapsed, 0.25)
        bridge._execute_openai_compatible_chat.assert_awaited_once()
        self.assertEqual(result.get("attempted_models"), ["kimi-coding"])

    def test_execute_builder_prewrite_timeout_skips_same_model_retry(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "kimi_api_key": "sk-kimi-test",
            "node_model_preferences": {
                "builder": ["gpt-5.4", "kimi-coding"],
            },
        })
        bridge._litellm = object()
        bridge._execute_litellm_tools = AsyncMock(return_value={
            "success": False,
            "output": "",
            "error": "builder pre-write timeout after 75s: no real file write or tool progress was produced.",
        })
        bridge._execute_openai_compatible_chat = AsyncMock(return_value={
            "success": True,
            "output": "ok",
            "tool_results": [],
            "mode": "openai_compatible_chat",
        })

        result = asyncio.run(
            bridge.execute(
                node={"type": "builder", "model": "gpt-5.4", "model_is_default": True},
                plugins=[MagicMock()],
                input_data=(
                    "Build an 8-page travel website.\n"
                    "Assigned HTML filenames: index.html, pricing.html, features.html, about.html."
                ),
                model="gpt-5.4",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("model"), "kimi-coding")
        self.assertEqual(result.get("attempted_models"), ["gpt-5.4", "kimi-coding"])
        self.assertEqual(bridge._execute_litellm_tools.await_count, 1)
        self.assertEqual(bridge._execute_openai_compatible_chat.await_count, 1)

    def test_execute_polisher_loop_guard_does_not_fallback_to_next_model(self):
        bridge = AIBridge(config={
            "openai_api_key": "sk-openai-test",
            "kimi_api_key": "sk-kimi-test",
            "node_model_preferences": {
                "polisher": ["kimi-coding", "gpt-5.4"],
            },
        })
        bridge._execute_openai_compatible = AsyncMock(return_value={
            "success": False,
            "output": "",
            "error": "polisher loop guard triggered after 4 non-write tool iterations without any file write.",
        })
        bridge._execute_litellm_chat = AsyncMock(return_value={
            "success": True,
            "output": "should not run",
            "tool_results": [],
            "mode": "litellm_chat",
        })

        result = asyncio.run(
            bridge.execute(
                node={"type": "polisher", "model": "kimi-coding", "model_is_default": True},
                plugins=[MagicMock()],
                input_data="Polish the current multi-page site under /tmp/evermind_output/.",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("model"), "kimi-coding")
        self.assertEqual(result.get("attempted_models"), ["kimi-coding"])
        self.assertEqual(bridge._execute_openai_compatible.await_count, 1)
        self.assertEqual(bridge._execute_litellm_chat.await_count, 0)


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
        self.assertEqual(self.bridge._max_tokens_for_node("builder"), 16384)
        self.assertEqual(self.bridge._timeout_for_node("builder"), 900)

    def test_non_builder_defaults_are_lower(self):
        self.assertEqual(self.bridge._max_tokens_for_node("tester"), 4096)
        self.assertEqual(self.bridge._timeout_for_node("tester"), 90)
        self.assertEqual(self.bridge._max_tool_iterations_for_node("tester"), 10)
        self.assertEqual(self.bridge._max_tool_iterations_for_node("reviewer"), 10)
        self.assertEqual(self.bridge._max_tool_iterations_for_node("analyst"), 3)

    def test_asset_plan_nodes_use_compact_budgets(self):
        self.assertEqual(self.bridge._max_tokens_for_node("spritesheet"), 2048)
        self.assertEqual(self.bridge._timeout_for_node("spritesheet"), 75)
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("spritesheet"), 45)
        self.assertEqual(self.bridge._max_tokens_for_node("assetimport"), 2048)
        self.assertEqual(self.bridge._timeout_for_node("assetimport"), 75)
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("assetimport"), 45)

    def test_env_overrides_are_clamped(self):
        with patch.dict("os.environ", {
            "EVERMIND_BUILDER_MAX_TOKENS": "999999",
            "EVERMIND_BUILDER_TIMEOUT_SEC": "5",
            "EVERMIND_MAX_TOKENS": "-1",
            "EVERMIND_TIMEOUT_SEC": "999",
            "EVERMIND_BUILDER_MAX_TOOL_ITERS": "100",
            "EVERMIND_QA_MAX_TOOL_ITERS": "100",
            "EVERMIND_ANALYST_MAX_TOOL_ITERS": "1",
            "EVERMIND_DEFAULT_MAX_TOOL_ITERS": "0",
        }):
            self.assertEqual(self.bridge._max_tokens_for_node("builder"), 32768)
            self.assertEqual(self.bridge._timeout_for_node("builder"), 30)
            self.assertEqual(self.bridge._max_tokens_for_node("tester"), 1024)
            self.assertEqual(self.bridge._timeout_for_node("tester"), 600)
            self.assertEqual(self.bridge._max_tool_iterations_for_node("builder"), 20)
            self.assertEqual(self.bridge._max_tool_iterations_for_node("tester"), 12)
            self.assertEqual(self.bridge._max_tool_iterations_for_node("analyst"), 2)

    def test_analyst_browser_limit_defaults_to_two(self):
        self.assertEqual(self.bridge._analyst_browser_call_limit(), 2)
        self.assertFalse(self.bridge._should_block_browser_call("analyst", {"browser": 1}))
        self.assertTrue(self.bridge._should_block_browser_call("analyst", {"browser": 2}))
        self.assertFalse(self.bridge._should_block_browser_call("builder", {"browser": 99}))

    def test_analyst_browser_limit_can_be_overridden(self):
        with patch.dict("os.environ", {"EVERMIND_ANALYST_MAX_BROWSER_CALLS": "1"}):
            self.assertEqual(self.bridge._analyst_browser_call_limit(), 1)
            self.assertTrue(self.bridge._should_block_browser_call("analyst", {"browser": 1}))

    def test_analyst_system_prompt_requires_live_browser_research(self):
        prompt = AGENT_PRESETS["analyst"]["instructions"]
        self.assertIn("MUST use the browser tool", prompt)
        self.assertIn("Visit at most 2 distinct URLs", prompt)
        self.assertIn("visited URLs", prompt)
        self.assertIn("do NOT browse playable web games", prompt)
        self.assertIn("deliverables_contract", prompt)
        self.assertIn("risk_register", prompt)

    def test_router_prompt_exposes_specialized_agents(self):
        prompt = AGENT_PRESETS["router"]["instructions"]
        self.assertIn("scribe", prompt)
        self.assertIn("imagegen", prompt)
        self.assertIn("spritesheet", prompt)
        self.assertIn("GitHub repos", prompt)

    def test_asset_plan_prompts_require_compact_json(self):
        self.assertIn("compact JSON", AGENT_PRESETS["spritesheet"]["instructions"])
        self.assertIn("No prose", AGENT_PRESETS["spritesheet"]["instructions"])
        self.assertIn("compact JSON", AGENT_PRESETS["assetimport"]["instructions"])

    def test_stream_stall_timeout_defaults_are_role_aware(self):
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("builder"), 180)
        self.assertEqual(self.bridge._stream_stall_timeout_for_node("tester"), 180)

    def test_effective_builder_stream_stall_timeout_tightens_for_multi_page_retry(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, materials.html, heritage.html, boutiques.html, contact.html.\n"
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 1/3): Multi-page delivery incomplete."
        )
        self.assertEqual(self.bridge._effective_stream_stall_timeout("builder", input_data), 150)

    def test_effective_builder_stream_stall_timeout_tightens_for_large_multi_page_initial_run(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, materials.html, heritage.html, boutiques.html, contact.html."
        )
        self.assertEqual(self.bridge._effective_stream_stall_timeout("builder", input_data), 180)

    def test_stream_stall_timeout_env_overrides_are_clamped(self):
        with patch.dict("os.environ", {
            "EVERMIND_BUILDER_STREAM_STALL_SEC": "9999",
            "EVERMIND_STREAM_STALL_SEC": "5",
        }):
            self.assertEqual(self.bridge._stream_stall_timeout_for_node("builder"), 600)
            self.assertEqual(self.bridge._stream_stall_timeout_for_node("tester"), 30)

    def test_builder_prewrite_call_timeout_defaults_to_120_for_multi_page(self):
        input_data = "做一个 8 页面奢侈品官网，包含 index.html, brand.html, collections.html, contact.html。"
        self.assertEqual(self.bridge._builder_prewrite_call_timeout("builder", input_data), 120)

    def test_builder_prewrite_call_timeout_tightens_retry_window(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "PREVIOUS ATTEMPT FAILED (retry 1/3): Multi-page delivery incomplete."
        )
        self.assertEqual(self.bridge._builder_prewrite_call_timeout("builder", input_data), 120)

    def test_builder_force_text_threshold_tightens_for_multi_page(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, materials.html, heritage.html, boutiques.html, contact.html."
        )
        self.assertEqual(self.bridge._builder_force_text_threshold(input_data), 2)

    def test_builder_force_text_threshold_allows_one_recovery_turn_on_retry(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, materials.html, heritage.html, boutiques.html, contact.html.\n"
            "⚠️ PREVIOUS ATTEMPT FAILED (retry 1/3): Multi-page delivery incomplete."
        )
        self.assertEqual(self.bridge._builder_force_text_threshold(input_data), 2)

    def test_builder_direct_multifile_requested_only_for_builder_with_marker(self):
        self.assertTrue(
            self.bridge._builder_direct_multifile_requested(
                "builder",
                "⚠️ MULTI-PAGE DELIVERY INCOMPLETE.\nDIRECT MULTI-FILE DELIVERY ONLY.\nReturn pricing.html and about.html.",
            )
        )
        self.assertFalse(
            self.bridge._builder_direct_multifile_requested(
                "reviewer",
                "DIRECT MULTI-FILE DELIVERY ONLY.",
            )
        )

    def test_builder_assigned_html_targets_honor_override_marker(self):
        targets = self.bridge._builder_assigned_html_targets(
            "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html.\n"
            "HTML TARGET OVERRIDE: index.html, about.html\n"
            "Return only the missing pages."
        )
        self.assertEqual(targets, ["index.html", "about.html"])

    def test_apply_runtime_node_contracts_prefers_node_allowed_html_targets(self):
        updated = self.bridge._apply_runtime_node_contracts(
            {
                "type": "builder",
                "allowed_html_targets": ["index.html", "pricing.html", "about.html"],
                "can_write_root_index": True,
            },
            "Assigned HTML filenames for this builder: index.html.\nReturn the homepage.",
        )
        self.assertIn("HTML TARGET OVERRIDE: index.html, pricing.html, about.html", updated)
        self.assertEqual(
            self.bridge._builder_assigned_html_targets(updated),
            ["index.html", "pricing.html", "about.html"],
        )

    def test_execute_honors_builder_direct_multifile_node_mode(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                    "builder_delivery_mode": "direct_multifile",
                },
                plugins=[],
                input_data="Return pricing.html and about.html as fenced HTML blocks.",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        bridge._execute_openai_compatible_chat.assert_awaited_once()
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_auto_routes_kimi_multi_page_builder_to_direct_multifile(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                },
                plugins=[],
                input_data=(
                    "做一个 8 页面轻奢品牌网站。\n"
                    "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
                    "craft.html, materials.html, journal.html, contact.html, faq.html."
                ),
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        bridge._execute_openai_compatible_chat.assert_awaited_once()
        bridge._execute_openai_compatible.assert_not_called()

    def test_execute_keeps_kimi_single_page_builder_on_tool_path(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "ok", "tool_results": [], "mode": "openai_compatible"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                },
                plugins=[],
                input_data="创建一个简单的个人网站首页。",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        bridge._execute_openai_compatible.assert_awaited_once()
        bridge._execute_openai_compatible_chat.assert_not_called()

    def test_execute_auto_routes_kimi_single_page_game_builder_to_direct_text(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._check_api_key = MagicMock(return_value=None)
        bridge._execute_openai_compatible_chat = AsyncMock(
            return_value={"success": True, "output": "```html index.html\\n<!DOCTYPE html><html><body>ok</body></html>\\n```", "tool_results": [], "mode": "openai_compatible_chat"}
        )
        bridge._execute_openai_compatible = AsyncMock(
            return_value={"success": True, "output": "wrong path", "tool_results": [], "mode": "openai_compatible"}
        )

        result = asyncio.run(
            bridge.execute(
                node={
                    "type": "builder",
                    "model": "kimi-coding",
                },
                plugins=[],
                input_data="创建一个我的世界风格的 3D 像素射击游戏，单页 index.html 即可。",
                model="kimi-coding",
                on_progress=None,
            )
        )

        self.assertTrue(result.get("success"))
        bridge._execute_openai_compatible_chat.assert_awaited_once()
        bridge._execute_openai_compatible.assert_not_called()

    def test_builder_should_auto_direct_multifile_only_for_kimi_multi_page_builder(self):
        input_data = (
            "做一个 8 页面轻奢品牌网站。\n"
            "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
            "craft.html, materials.html, journal.html, contact.html, faq.html."
        )
        self.assertTrue(
            self.bridge._builder_should_auto_direct_multifile(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )
        self.assertFalse(
            self.bridge._builder_should_auto_direct_multifile(
                "reviewer",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )
        self.assertFalse(
            self.bridge._builder_should_auto_direct_multifile(
                "builder",
                model_name="gpt-4o",
                input_data=input_data,
            )
        )

    def test_builder_should_auto_direct_multifile_for_single_override_target_on_kimi(self):
        input_data = (
            "做一个 8 页面旅游网站。\n"
            "HTML TARGET OVERRIDE: faq.html\n"
            "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, "
            "solutions.html, platform.html, contact.html, about.html, faq.html."
        )
        self.assertTrue(
            self.bridge._builder_should_auto_direct_multifile(
                "builder",
                model_name="kimi-coding",
                input_data=input_data,
            )
        )

    def test_builder_direct_multifile_budget_boosts_large_multi_page_delivery(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, "
            "materials.html, heritage.html, boutiques.html, contact.html."
        )
        self.assertEqual(
            self.bridge._builder_direct_multifile_budget(
                input_data,
                max_tokens=8192,
                timeout_sec=240,
            ),
            (14336, 420),
        )

    def test_builder_direct_multifile_budget_boosts_from_assigned_targets_even_if_classifier_misses(self):
        input_data = (
            "Assigned HTML filenames for this builder: index.html, brand.html, craftsmanship.html, collections.html, "
            "materials.html, heritage.html, boutiques.html, contact.html."
        )
        with patch("ai_bridge.task_classifier.wants_multi_page", return_value=False), \
             patch("ai_bridge.task_classifier.requested_page_count", return_value=0):
            self.assertEqual(
                self.bridge._builder_direct_multifile_budget(
                    input_data,
                    max_tokens=8192,
                    timeout_sec=240,
                ),
                (14336, 420),
            )

    def test_builder_direct_multifile_budget_uses_assigned_subset_not_whole_site_size(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, collections.html, contact.html."
        )
        self.assertEqual(
            self.bridge._builder_direct_multifile_budget(
                input_data,
                max_tokens=8192,
                timeout_sec=240,
            ),
            (12288, 300),
        )

    def test_builder_direct_multifile_batch_sizes_follow_assigned_subset_not_whole_site_size(self):
        input_data = (
            "做一个 8 页面奢侈品官网。\n"
            "Assigned HTML filenames for this builder: index.html, brand.html, collections.html, contact.html."
        )
        self.assertEqual(self.bridge._builder_direct_multifile_initial_batch_size(input_data), 2)
        self.assertEqual(self.bridge._builder_direct_multifile_batch_size(input_data), 2)


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

    def test_builder_forced_text_prompt_uses_multi_file_contract_for_multi_page(self):
        prompt = self.bridge._builder_forced_text_prompt("做一个奢侈品英文官网，一共 8 页")
        self.assertIn("MULTI-PAGE website request", prompt)
        self.assertIn("```html index.html", prompt)
        self.assertIn("additional linked HTML files", prompt)

    def test_builder_forced_text_prompt_mentions_assigned_filenames(self):
        prompt = self.bridge._builder_forced_text_prompt(
            "YOUR JOB: This is a MULTI-PAGE website request. "
            "You MUST create /tmp/evermind_output/index.html and 3 additional linked page(s). "
            "Otherwise use this non-overlapping fallback set for your secondary pages: collections.html, heritage.html, contact.html."
        )
        self.assertIn("Your assigned HTML filenames are: index.html, collections.html, heritage.html, contact.html.", prompt)
        self.assertIn("Unnamed ```html``` blocks are invalid", prompt)
        self.assertIn("/tmp/evermind_output/index.html", prompt)

    def test_builder_forced_text_prompt_for_secondary_builder_forbids_index(self):
        prompt = self.bridge._builder_forced_text_prompt(
            "YOUR JOB: This is a MULTI-PAGE website request. "
            "Do NOT write /tmp/evermind_output/index.html. "
            "Otherwise use this non-overlapping fallback set: about.html, platform.html, contact.html, faq.html."
        )
        self.assertIn("Do NOT emit ```html index.html```", prompt)
        self.assertIn("/tmp/evermind_output/about.html", prompt)
        self.assertIn("Return ONLY the assigned HTML files listed above.", prompt)
        self.assertNotIn("Return index.html plus", prompt)

    def test_builder_assigned_targets_parse_assigned_html_line(self):
        targets = self.bridge._builder_assigned_html_targets(
            "Assigned HTML filenames for this builder: index.html, brand.html, collections.html, contact.html."
        )
        self.assertEqual(targets, ["index.html", "brand.html", "collections.html", "contact.html"])

    def test_builder_returned_html_targets_reads_fence_headers(self):
        output = (
            "Looking at this request, I will build it now.\n\n"
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```\n"
            "```html about.html\n<!DOCTYPE html><html><body>about</body></html>\n```"
        )
        self.assertEqual(
            self.bridge._builder_returned_html_targets(output),
            ["index.html", "about.html"],
        )

    def test_builder_missing_html_targets_detects_remaining_pages(self):
        input_data = (
            "Assigned HTML filenames for this builder: index.html, about.html, contact.html."
        )
        output = "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```"
        self.assertEqual(
            self.bridge._builder_missing_html_targets(input_data, output),
            ["about.html", "contact.html"],
        )

    def test_builder_missing_html_targets_ignores_truncated_trailing_html_block(self):
        input_data = (
            "Assigned HTML filenames for this builder: index.html, about.html, contact.html."
        )
        output = (
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```\n"
            "```html about.html\n<!DOCTYPE html><html><body>about</body></html>\n```\n"
            "```html contact.html\n<!DOCTYPE html><html><body>contact"
        )
        self.assertEqual(
            self.bridge._builder_missing_html_targets(input_data, output),
            ["contact.html"],
        )

    def test_builder_direct_multifile_continuation_prompt_targets_only_remaining_files(self):
        messages = self.bridge._builder_direct_multifile_continuation_messages(
            "system prompt",
            "Assigned HTML filenames for this builder: index.html, about.html, contact.html.",
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```",
            ["about.html", "contact.html"],
        )
        self.assertEqual(len(messages), 2)
        prompt = messages[1]["content"]
        self.assertIn("Already returned: index.html", prompt)
        self.assertIn("ONLY this next batch of HTML files: about.html, contact.html", prompt)
        self.assertIn("The ONLY valid local HTML route set for this site is: index.html, about.html, contact.html", prompt)
        self.assertIn("Rewrite or remove any local href that points to a non-assigned HTML filename", prompt)
        self.assertIn("Do NOT restart from index.html", prompt)
        self.assertIn("HTML TARGET OVERRIDE: about.html, contact.html", prompt)
        self.assertIn("Shared assets are still missing", prompt)

    def test_builder_direct_multifile_initial_prompt_limits_first_batch(self):
        messages = self.bridge._builder_direct_multifile_initial_messages(
            "system prompt",
            (
                "做一个 8 页面工艺品牌站。\n"
                "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
                "craft.html, materials.html, journal.html, contact.html, faq.html."
            ),
        )
        self.assertEqual(len(messages), 2)
        prompt = messages[1]["content"].split("[DIRECT MULTI-FILE INITIAL DELIVERY]", 1)[1]
        self.assertIn("HTML TARGET OVERRIDE: index.html", prompt)
        self.assertIn("Return ONLY this first batch now: index.html", prompt)
        self.assertIn("The ONLY valid local HTML route set for this site is: index.html, about.html, collections.html, craft.html, materials.html, journal.html, contact.html, faq.html", prompt)
        self.assertIn("Every internal href that points to a local .html page MUST use one of those exact filenames.", prompt)
        self.assertIn("another continuation will request them immediately", prompt)
        self.assertNotIn("Return ONLY this first batch now: index.html, about.html", prompt)
        self.assertIn("shared ```css styles.css``` block", prompt)
        self.assertIn("You MUST return ```css styles.css``` and ```js app.js```", prompt)
        self.assertIn("Do NOT inline large CSS or JS blobs into the HTML", prompt)

    def test_builder_direct_multifile_continuation_limit_scales_for_large_single_page_batches(self):
        limit = self.bridge._builder_direct_multifile_continuation_limit(
            (
                "做一个 8 页面工艺品牌站。\n"
                "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
                "craft.html, materials.html, journal.html, contact.html, faq.html."
            ),
        )
        self.assertEqual(limit, 4)

    def test_builder_direct_multifile_large_continuation_batch_size_defaults_to_two(self):
        batch_size = self.bridge._builder_direct_multifile_batch_size(
            (
                "做一个 8 页面工艺品牌站。\n"
                "Assigned HTML filenames for this builder: index.html, about.html, collections.html, "
                "craft.html, materials.html, journal.html, contact.html, faq.html."
            ),
        )
        self.assertEqual(batch_size, 2)

    def test_builder_direct_multifile_continuation_prompt_forbids_repeating_shared_assets(self):
        messages = self.bridge._builder_direct_multifile_continuation_messages(
            "system prompt",
            "Assigned HTML filenames for this builder: index.html, pricing.html.",
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```",
            ["pricing.html"],
        )
        prompt = messages[1]["content"]
        self.assertIn("Return ONLY the next HTML file(s) in this batch.", prompt)
        self.assertIn("Do NOT re-emit styles.css or app.js", prompt)
        self.assertIn("Do NOT inline large CSS or JS into this continuation page", prompt)

    @patch("openai.OpenAI")
    def test_openai_compatible_chat_continues_direct_multifile_builder_until_missing_pages_return(self, mock_openai):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        client = MagicMock()
        mock_openai.return_value = client

        def _make_stream_chunks(content: str, finish_reason: str = "stop", usage=None):
            """Create a list of streaming chunks that simulate the OpenAI streaming API."""
            chunks = []
            # Content chunk
            chunks.append(SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=content, tool_calls=None),
                    finish_reason=None,
                )],
            ))
            # Final chunk with finish_reason
            chunks.append(SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=None),
                    finish_reason=finish_reason,
                )],
            ))
            # Usage chunk (no choices)
            if usage:
                chunks.append(SimpleNamespace(choices=[], usage=SimpleNamespace(**usage)))
            return iter(chunks)

        first_stream = _make_stream_chunks(
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```",
            finish_reason="length",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        second_stream = _make_stream_chunks(
            "```html about.html\n<!DOCTYPE html><html><body>about</body></html>\n```",
            finish_reason="stop",
            usage={"prompt_tokens": 40, "completion_tokens": 30, "total_tokens": 70},
        )
        client.chat.completions.create.side_effect = [first_stream, second_stream]

        result = asyncio.run(
            bridge._execute_openai_compatible_chat(
                {
                    "type": "builder",
                    "builder_delivery_mode": "direct_multifile",
                },
                (
                    "做一个 2 页面品牌官网。\n"
                    "Assigned HTML filenames for this builder: index.html, about.html."
                ),
                {
                    "litellm_id": "openai/kimi-coding",
                    "provider": "kimi",
                    "api_base": "https://api.moonshot.test/v1",
                    "extra_headers": {},
                },
                on_progress=None,
            )
        )

        self.assertTrue(result["success"])
        self.assertIn("index.html", result["output"])
        self.assertIn("about.html", result["output"])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(result["usage"]["total_tokens"], 220)

    @patch("openai.OpenAI")
    def test_openai_compatible_chat_timeout_salvages_prior_direct_multifile_batches(self, mock_openai):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        client = MagicMock()
        mock_openai.return_value = client

        def _make_stream_chunks(content: str, finish_reason: str = "stop", usage=None):
            chunks = [
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=content, tool_calls=None),
                        finish_reason=None,
                    )],
                ),
            ]
            if finish_reason:
                chunks.append(SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason=finish_reason,
                    )],
                ))
            if usage:
                chunks.append(SimpleNamespace(choices=[], usage=SimpleNamespace(**usage)))
            return iter(chunks)

        def _timing_out_stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(
                        content="```html about.html\n<!DOCTYPE html><html><body>about</body></html>\n```",
                        tool_calls=None,
                    ),
                    finish_reason=None,
                )],
            )
            raise TimeoutError("Chat stream stalled: no chunk for 180s")

        client.chat.completions.create.side_effect = [
            _make_stream_chunks(
                "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```",
                finish_reason="length",
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            ),
            _timing_out_stream(),
        ]

        result = asyncio.run(
            bridge._execute_openai_compatible_chat(
                {
                    "type": "builder",
                    "builder_delivery_mode": "direct_multifile",
                },
                (
                    "做一个 2 页面品牌官网。\n"
                    "Assigned HTML filenames for this builder: index.html, about.html."
                ),
                {
                    "litellm_id": "openai/kimi-coding",
                    "provider": "kimi",
                    "api_base": "https://api.moonshot.test/v1",
                    "extra_headers": {},
                },
                on_progress=None,
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "openai_compatible_chat_timeout_salvage")
        self.assertIn("index.html", result["output"])
        self.assertIn("about.html", result["output"])

    @patch("openai.OpenAI")
    def test_openai_compatible_chat_progress_timeout_does_not_delay_api_dispatch(self, mock_openai):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        client = MagicMock()
        mock_openai.return_value = client

        def _make_stream_chunks(content: str, finish_reason: str = "stop", usage=None):
            chunks = [
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=content, tool_calls=None),
                        finish_reason=None,
                    )],
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason=finish_reason,
                    )],
                ),
            ]
            if usage:
                chunks.append(SimpleNamespace(choices=[], usage=SimpleNamespace(**usage)))
            return iter(chunks)

        client.chat.completions.create.return_value = _make_stream_chunks(
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```",
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

        async def slow_progress(_payload):
            await asyncio.sleep(0.5)

        start = time.perf_counter()
        with patch.dict("os.environ", {"EVERMIND_PROGRESS_EVENT_TIMEOUT_SEC": "0.01"}):
            result = asyncio.run(
                bridge._execute_openai_compatible_chat(
                    {
                        "type": "builder",
                        "builder_delivery_mode": "direct_multifile",
                    },
                    "做一个 1 页面品牌官网。\nAssigned HTML filenames for this builder: index.html.",
                    {
                        "litellm_id": "openai/kimi-coding",
                        "provider": "kimi",
                        "api_base": "https://api.moonshot.test/v1",
                        "extra_headers": {},
                    },
                    on_progress=slow_progress,
                )
            )
        elapsed = time.perf_counter() - start

        self.assertTrue(result["success"])
        self.assertLess(elapsed, 0.25)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_builder_tool_repair_prompt_redirects_missing_read_to_write(self):
        prompt = self.bridge._builder_tool_repair_prompt(
            "Save to /tmp/evermind_output/index.html via file_ops write.",
            "read",
            {"success": False, "error": "File not found: /tmp/evermind_output/index.html"},
        )
        self.assertIsNotNone(prompt)
        self.assertIn("Create it now with file_ops write", prompt)
        self.assertIn("/tmp/evermind_output/index.html", prompt)

    def test_builder_tool_repair_prompt_rejects_blank_security_path(self):
        prompt = self.bridge._builder_tool_repair_prompt(
            "Otherwise use this non-overlapping fallback set: pricing.html, about.html.",
            "write",
            {"success": False, "error": "Path not allowed by security policy: "},
        )
        self.assertIsNotNone(prompt)
        self.assertIn("explicit absolute paths", prompt)
        self.assertIn("pricing.html, about.html", prompt)

    def test_builder_tool_repair_prompt_handles_unassigned_html_target(self):
        prompt = self.bridge._builder_tool_repair_prompt(
            "Assigned HTML filenames: about.html, platform.html, contact.html, faq.html.",
            "write",
            {
                "success": False,
                "error": "HTML target not assigned for builder: index.html. Allowed HTML filenames: about.html, platform.html, contact.html, faq.html",
            },
        )
        self.assertIsNotNone(prompt)
        self.assertIn("not assigned to you", prompt)
        self.assertIn("about.html, platform.html, contact.html, faq.html", prompt)

    def test_builder_non_write_followup_prompt_forces_write_after_successful_list(self):
        prompt = self.bridge._builder_non_write_followup_prompt(
            (
                "做一个 4 页品牌网站。 "
                "Assigned HTML filenames: index.html, pricing.html, about.html, contact.html."
            ),
            "list",
            {"success": True, "data": {"entries": []}},
            1,
        )
        self.assertIsNotNone(prompt)
        self.assertIn("VERY NEXT response must be one or more file_ops write calls", prompt)
        self.assertIn("index.html, pricing.html, about.html, contact.html", prompt)
        self.assertIn("covers EVERY assigned HTML filename", prompt)
        self.assertIn("A one-page retry is still a failure", prompt)

    def test_builder_system_prompt_is_task_adaptive_for_game(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "builder"},
            plugins=[],
            input_data="请帮我做一个贪吃蛇小游戏",
        )
        self.assertIn("GAME DESIGN SYSTEM", prompt)
        self.assertIn("[Skill: gameplay-foundation]", prompt)
        self.assertIn("first successful write must already contain visible <body> content", prompt.lower())

    def test_select_builder_salvage_text_prefers_longer_recovered_html(self):
        latest = "<!DOCTYPE html><html><head><style>body{margin:0}</style></head><body>"
        recovered = (
            "<!DOCTYPE html><html><head><title>Game</title></head>"
            "<body><main><section><h1>Voxel Strike</h1><button>Start</button>"
            "<canvas id='game'></canvas><div class='hud'>HP 100</div></section></main></body></html>"
        )

        selected = self.bridge._select_builder_salvage_text(latest, "", recovered)

        self.assertEqual(selected, recovered)

    def test_builder_prompt_includes_active_skill_checklist(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "builder"},
            plugins=[],
            input_data="做一个带动画的品牌官网，需要插画 hero",
        )
        self.assertIn("MANDATORY SKILL DIRECTIVES", prompt)
        self.assertIn("ACTIVE SKILLS", prompt)
        self.assertIn("motion-choreography-system", prompt)

    def test_reviewer_prompt_loads_browser_testing_skill(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "reviewer"},
            plugins=[],
            input_data="做一个科技风官网",
        )
        self.assertIn("[Skill: browser-observe-act-verify]", prompt)
        self.assertIn("direct preview-path visits are acceptable", prompt)

    def test_analyst_prompt_loads_research_skill(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "analyst"},
            plugins=[],
            input_data="做一个科技风官网",
        )
        self.assertIn("[Skill: research-pattern-extraction]", prompt)
        self.assertIn("[Skill: source-first-research-loop]", prompt)

    def test_polisher_prompt_loads_scroll_evidence_skill(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "polisher"},
            plugins=[],
            input_data="把现有奢侈品官网做得更高级，补充转场和滚动动效",
        )
        self.assertIn("[Skill: scroll-evidence-capture]", prompt)
        self.assertIn("[Skill: visual-slot-recovery]", prompt)
        self.assertIn("upgrade an existing artifact", prompt.lower())
        self.assertIn("[Collection Image]", prompt)

    def test_imagegen_prompt_loads_image_direction_skills(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="生成一张品牌海报和封面图片",
        )
        self.assertIn("[Skill: image-prompt-director]", prompt)
        self.assertIn("[Skill: visual-storyboard-shotlist]", prompt)
        self.assertIn("[Skill: comfyui-pipeline-brief]", prompt)

    def test_imagegen_prompt_mentions_comfyui_when_available(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="为像素风平台跳跃游戏生成角色和敌人素材",
        )
        self.assertIn("comfyui plugin", prompt.lower())

    def test_imagegen_prompt_marks_backend_missing_when_unconfigured(self):
        bridge = AIBridge(config={})
        prompt = bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="生成一套像素风角色素材",
        )
        self.assertIn("no configured image backend detected", prompt.lower())

    def test_imagegen_prompt_marks_backend_configured_when_available(self):
        bridge = AIBridge(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        })
        prompt = bridge._compose_system_prompt(
            {"type": "imagegen"},
            plugins=[],
            input_data="生成一套像素风角色素材",
        )
        self.assertIn("configured image backend detected", prompt.lower())

    def test_reviewer_preset_uses_strict_thresholds(self):
        prompt = AGENT_PRESETS["reviewer"]["instructions"]
        self.assertIn("Any single dimension < 5", prompt)
        self.assertIn("blocking_issues", prompt)
        self.assertIn("ship_readiness", prompt)
        self.assertIn("missing_deliverables", prompt)

    def test_scribe_prompt_loads_doc_skills(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "scribe"},
            plugins=[],
            input_data="写一份 API documentation 和 README",
        )
        self.assertIn("[Skill: docs-clarity-architecture]", prompt)

    def test_presentation_builder_prompt_loads_export_skill(self):
        prompt = self.bridge._compose_system_prompt(
            {"type": "builder"},
            plugins=[],
            input_data="做一个融资路演 PPT slides",
        )
        self.assertIn("[Skill: pptx-export-bridge]", prompt)

    def test_builder_repo_edit_prompt_injects_aider_style_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"scripts":{"build":"next build","test":"vitest"}}', encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app").mkdir()
            (root / "src" / "app" / "page.tsx").write_text("export default function Page(){ return null; }", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "app.test.ts").write_text("it('works', () => {})", encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir})
            prompt = bridge._compose_system_prompt(
                {"type": "builder"},
                plugins=[],
                input_data="修复这个仓库里的登录页面 bug，并保持现有 Next.js 结构",
            )
            self.assertIn("EXISTING REPOSITORY EDIT MODE", prompt)
            self.assertIn("AIDER-STYLE REPO MAP", prompt)
            self.assertIn("src/app/page.tsx", prompt)
            self.assertIn("npm run build", prompt)

    def test_greenfield_builder_prompt_does_not_inject_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"scripts":{"build":"next build"}}', encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir})
            prompt = bridge._compose_system_prompt(
                {"type": "builder"},
                plugins=[],
                input_data="做一个全新的品牌官网首页",
            )
            self.assertNotIn("AIDER-STYLE REPO MAP", prompt)

    def test_greenfield_file_ops_delivery_builder_prompt_does_not_inject_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text('{"scripts":{"build":"next build","lint":"next lint"}}', encoding="utf-8")
            (root / "frontend" / "src").mkdir()
            (root / "frontend" / "src" / "app").mkdir(parents=True, exist_ok=True)
            (root / "frontend" / "src" / "app" / "page.tsx").write_text("export default function Page(){ return null; }", encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir, "output_dir": "/var/folders/demo/evermind_output"})
            prompt = bridge._compose_system_prompt(
                {"type": "builder"},
                plugins=[],
                input_data=(
                    "Build a commercial-grade multi-page website for a luxury brand. "
                    "Create index.html plus at least 7 additional linked HTML page(s) via file_ops write."
                ),
            )
            self.assertNotIn("AIDER-STYLE REPO MAP", prompt)
            self.assertIn("RUNTIME OUTPUT CONTRACT", prompt)

    def test_debugger_repo_edit_prompt_injects_repo_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / "server.py").write_text("print('hi')\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_server.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            bridge = AIBridge(config={"workspace": tmpdir})
            prompt = bridge._compose_system_prompt(
                {"type": "debugger"},
                plugins=[],
                input_data="修复当前仓库里 failing test 的 root cause",
            )
            self.assertIn("AIDER-STYLE REPO MAP", prompt)
            self.assertIn("server.py", prompt)

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

    def test_builder_partial_text_is_salvageable_for_named_multi_page_html(self):
        text = (
            "```html index.html\n<!DOCTYPE html><html><body>home</body></html>\n```\n"
            "```html collections.html\n<!DOCTYPE html><html><body>collections</body></html>\n```"
        )
        self.assertTrue(
            self.bridge._builder_partial_text_is_salvageable(
                "Create index.html plus at least 7 additional linked HTML page(s) via file_ops write.",
                text,
            )
        )

    def test_builder_partial_text_is_not_salvageable_for_short_prose(self):
        self.assertFalse(
            self.bridge._builder_partial_text_is_salvageable(
                "Build a premium website",
                "I would create a homepage and maybe add sections later.",
            )
        )


class TestReviewerTesterFollowupPolicy(unittest.TestCase):
    def setUp(self):
        self.bridge = AIBridge(config={})

    def test_analyst_followup_requires_two_distinct_source_urls(self):
        reason = self.bridge._analyst_browser_followup_reason(
            "analyst",
            {"browser": 1},
            [{"success": True, "data": {"url": "https://example.com/docs"}}],
            available_tool_names={"browser"},
        )
        self.assertIn("only visited 1 source URL", reason)

    def test_analyst_followup_message_requires_browsing_before_finalize(self):
        msg = self.bridge._analyst_browser_followup_message(
            "You have only visited 1 source URL. Use the browser tool on one more distinct GitHub/doc/tutorial/source page before final report."
        )
        self.assertIn("one more distinct URL", msg)
        self.assertIn("Do not output the final analyst report yet", msg)

    def test_website_review_requires_post_interaction_verification(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "snapshot", "ok": True},
                {"action": "scroll", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
            ],
        )
        self.assertIn("record_scroll", reason)

    def test_website_review_requires_scroll_to_bottom_when_metadata_shows_more_content(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "snapshot", "ok": True},
                {"action": "scroll", "ok": True, "is_scrollable": True, "at_bottom": False},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "snapshot", "ok": True, "state_changed": True},
            ],
        )
        self.assertIn("bottom of the page", reason)

    def test_website_review_passes_with_post_interaction_snapshot(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "snapshot", "ok": True},
                {"action": "scroll", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "snapshot", "ok": True, "state_changed": True},
            ],
        )
        self.assertIsNone(reason)

    def test_website_review_accepts_record_scroll_as_post_interaction_verification(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {
                    "action": "record_scroll",
                    "ok": True,
                    "state_changed": True,
                    "at_bottom": True,
                    "is_scrollable": True,
                },
            ],
        )
        self.assertIsNone(reason)

    def test_multi_page_review_requires_distinct_page_visits(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                {"action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "is_scrollable": False},
                {"action": "act", "subaction": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/"},
                {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/"},
            ],
            "做一个三页面官网，包含首页、定价页和联系页",
        )
        self.assertIn("every requested page", reason)

    def test_multi_page_review_passes_after_visiting_all_pages(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                {"action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "at_bottom": True, "is_scrollable": True},
                {"action": "act", "subaction": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                {"action": "act", "subaction": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/contact.html"},
                {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/contact.html"},
            ],
            "做一个三页面官网，包含首页、定价页和联系页",
        )
        self.assertIsNone(reason)

    def test_multi_page_review_names_missing_pages_from_current_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for name in ("index.html", "pricing.html", "contact.html"):
                (out / name).write_text("<!doctype html><html><body></body></html>", encoding="utf-8")
            self.bridge.config["output_dir"] = td
            reason = self.bridge._review_browser_followup_reason(
                "reviewer",
                "website",
                [
                    {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                    {"action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "at_bottom": True, "is_scrollable": True},
                    {"action": "act", "subaction": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                    {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                ],
                "做一个三页面官网，包含首页、定价页和联系页",
            )
        self.assertIn("contact.html", reason)
        self.assertIn("2/3", reason)

    def test_review_followup_message_includes_direct_hint_for_missing_pages(self):
        msg = self.bridge._review_browser_followup_message(
            (
                "You must cover every requested page/route before final verdict. "
                "Current distinct pages visited: 2/3. Remaining missing pages: contact.html."
            ),
            "website",
        )
        self.assertIn("contact.html", msg)
        self.assertIn('"action":"navigate"', msg)

    def test_game_test_requires_press_sequence(self):
        reason = self.bridge._review_browser_followup_reason(
            "tester",
            "game",
            [
                {"action": "snapshot", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "snapshot", "ok": True, "state_changed": True},
            ],
        )
        self.assertIn("gameplay controls", reason)

    def test_game_review_requires_browser_use_when_available(self):
        self.bridge.config["qa_enable_browser_use"] = True
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "game",
            [
                {"action": "snapshot", "ok": True},
                {"action": "click", "ok": True, "state_changed": True},
                {"action": "press_sequence", "ok": True, "state_changed": True},
            ],
            tool_call_stats={"browser": 3},
            available_tool_names={"browser", "browser_use"},
        )
        self.assertIn("browser_use", reason)

    def test_followup_message_includes_browser_use_gameplay_hint(self):
        msg = self.bridge._review_browser_followup_message(
            "You must use browser_use for a real gameplay session before final verdict.",
            "game",
        )
        self.assertIn("browser_use", msg)
        self.assertIn('"url":"http://127.0.0.1:8765/preview/"', msg)

    def test_desktop_qa_browser_suppression_blocks_reviewer_browser_tools(self):
        self.assertTrue(
            self.bridge._desktop_qa_browser_suppressed(
                "reviewer",
                "[Desktop QA Session Evidence]\nDESKTOP_QA_BROWSER_SUPPRESSED=1\nUse the desktop QA record only.",
            )
        )
        self.assertFalse(
            self.bridge._desktop_qa_browser_suppressed(
                "builder",
                "[Desktop QA Session Evidence]\nDESKTOP_QA_BROWSER_SUPPRESSED=1\nUse the desktop QA record only.",
            )
        )

    def test_browser_use_action_events_normalize_to_snapshot_click_and_press(self):
        events = self.bridge._browser_use_action_events({
            "success": True,
            "data": {
                "final_url": "http://127.0.0.1:8765/preview/",
                "recording_path": "/tmp/gameplay.webm",
                "history_items": [
                    {
                        "step": 1,
                        "action_names": ["click_element"],
                        "url": "http://127.0.0.1:8765/preview/",
                        "screenshot_path": "/tmp/step1.png",
                        "errors": [],
                    },
                    {
                        "step": 2,
                        "action_names": ["send_keys"],
                        "url": "http://127.0.0.1:8765/preview/",
                        "screenshot_path": "/tmp/step2.png",
                        "errors": [],
                    },
                ],
            },
        })
        actions = [event.get("action") for event in events]
        self.assertIn("snapshot", actions)
        self.assertIn("click", actions)
        self.assertIn("press_sequence", actions)
        self.assertTrue(any(event.get("recording_path") == "/tmp/gameplay.webm" for event in events))

    def test_qa_browser_use_prefetch_summary_surfaces_recording_and_actions(self):
        summary = self.bridge._qa_browser_use_prefetch_summary(
            {
                "success": True,
                "data": {
                    "final_url": "http://127.0.0.1:8765/preview/",
                    "recording_path": "/tmp/gameplay.webm",
                    "capture_path": "/tmp/shot.png",
                },
            },
            [
                {"action": "snapshot"},
                {"action": "click"},
                {"action": "press_sequence"},
            ],
            "game",
        )
        self.assertIn("browser_use QA preflight", summary)
        self.assertIn("recording_path: /tmp/gameplay.webm", summary)
        self.assertIn("captured_actions: snapshot, click, press_sequence", summary)

    def test_followup_policy_accepts_observe_and_act(self):
        reason = self.bridge._review_browser_followup_reason(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True},
                {"action": "scroll", "ok": True},
                {"action": "act", "subaction": "click", "ok": True, "state_changed": True},
                {"action": "observe", "ok": True, "state_changed": True},
            ],
        )
        self.assertIsNone(reason)


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

    def test_analyst_browser_ignores_global_headful_context(self):
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

        self.bridge.config = {"browser_headful": True, "reviewer_tester_force_headful": True}
        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("browser", {"action": "navigate"}, [plugin], node_type="analyst"))
        self.assertEqual(out.get("ok"), True)
        self.assertFalse(plugin.last_context.get("browser_headful", False))
        self.assertIsNone(plugin.last_context.get("browser_force_reason"))

    def test_browser_plugin_receives_run_and_evidence_context(self):
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

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin(
            "browser",
            {"action": "navigate"},
            [plugin],
            node_type="tester",
            node={
                "type": "tester",
                "run_id": "run_ctx_1",
                "node_execution_id": "nodeexec_ctx_1",
            },
        ))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(out.get("_plugin"), "browser")
        self.assertEqual(plugin.last_context.get("run_id"), "run_ctx_1")
        self.assertEqual(plugin.last_context.get("node_execution_id"), "nodeexec_ctx_1")
        self.assertTrue(plugin.last_context.get("browser_save_evidence"))

    def test_reviewer_file_ops_runs_in_read_only_mode(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True, "mode": self.context.get("file_ops_mode")}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("file_ops", {"action": "list", "path": "/tmp"}, [plugin], node_type="reviewer"))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(plugin.last_context.get("file_ops_mode"), "read_only")
        self.assertEqual(plugin.last_context.get("file_ops_node_type"), "reviewer")

    def test_scribe_file_ops_runs_in_read_only_mode(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True, "mode": self.context.get("file_ops_mode")}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("file_ops", {"action": "list", "path": "/tmp"}, [plugin], node_type="scribe"))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(plugin.last_context.get("file_ops_mode"), "read_only")
        self.assertEqual(plugin.last_context.get("file_ops_node_type"), "scribe")

    def test_polisher_file_ops_keeps_write_mode(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True, "mode": self.context.get("file_ops_mode", "read_write")}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin("file_ops", {"action": "write", "path": "/tmp/evermind_output/index.html"}, [plugin], node_type="polisher"))
        self.assertEqual(out.get("ok"), True)
        self.assertEqual(plugin.last_context.get("file_ops_node_type"), "polisher")
        self.assertNotEqual(plugin.last_context.get("file_ops_mode"), "read_only")

    def test_polisher_can_write_deliverable_html_to_output_root(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.html"
            target.write_text(
                "<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body><main><section>Strong draft</section></main></body></html>",
                encoding="utf-8",
            )
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body><main><section>Polished result</section></main></body></html>",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "polisher",
                    "file_ops_output_dir": td,
                },
            ))

        self.assertTrue(out.success)
        self.assertTrue(out.data.get("written"))
        self.assertEqual(out.data.get("path"), str(target))

    def test_file_ops_write_blocked_in_read_only_mode(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            out = asyncio.run(self.bridge._run_plugin(
                "file_ops",
                {"action": "write", "path": str(Path(td) / "blocked.txt"), "content": "x"},
                [plugin],
                node_type="tester",
            ))
        self.assertFalse(out.get("success", True))
        self.assertIn("read-only", str(out.get("error", "")).lower())

    def test_non_builder_cannot_write_deliverable_html_to_output_root(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            self.bridge.config = {"allowed_dirs": [td], "output_dir": td}
            out = asyncio.run(self.bridge._run_plugin(
                "file_ops",
                {"action": "write", "path": str(Path(td) / "index.html"), "content": "<html></html>"},
                [plugin],
                node_type="imagegen",
            ))
        self.assertFalse(out.get("success", True))
        self.assertIn("not allowed to write deliverable html", str(out.get("error", "")).lower())

    def test_builder_file_ops_receives_assigned_html_targets_context(self):
        class ResultObj:
            def __init__(self, context):
                self.context = context

            def to_dict(self):
                return {"ok": True}

        class StubPlugin:
            name = "file_ops"

            def __init__(self):
                self.last_context = None

            async def execute(self, params, context=None):
                self.last_context = dict(context or {})
                return ResultObj(self.last_context)

        plugin = StubPlugin()
        out = asyncio.run(self.bridge._run_plugin(
            "file_ops",
            {"action": "write", "path": "/tmp/evermind_output/about.html", "content": "<html></html>"},
            [plugin],
            node_type="builder",
            node={
                "type": "builder",
                "output_dir": "/tmp/evermind_output",
                "allowed_html_targets": ["about.html", "platform.html", "contact.html", "faq.html"],
                "can_write_root_index": False,
            },
        ))
        self.assertTrue(out.get("ok"))
        self.assertEqual(
            plugin.last_context.get("file_ops_allowed_html_targets"),
            ["about.html", "platform.html", "contact.html", "faq.html"],
        )
        self.assertFalse(plugin.last_context.get("file_ops_can_write_root_index"))

    def test_builder_file_ops_blocks_unassigned_html_target(self):
        plugin = FileOpsPlugin()
        self.bridge.config = {"allowed_dirs": ["/tmp/evermind_output"]}
        out = asyncio.run(self.bridge._run_plugin(
                "file_ops",
                {"action": "write", "path": "/tmp/evermind_output/index.html", "content": "<html></html>"},
                [plugin],
                node_type="builder",
                node={
                    "type": "builder",
                    "output_dir": "/tmp/evermind_output",
                    "allowed_html_targets": ["about.html", "platform.html", "contact.html", "faq.html"],
                    "can_write_root_index": False,
                    "enforce_html_targets": True,
                },
            ))
        self.assertFalse(out.get("success", True))
        self.assertIn("HTML target not assigned for builder", str(out.get("error", "")))

    def test_builder_file_ops_blank_list_path_defaults_to_output_dir(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            out = asyncio.run(plugin.execute(
                {"action": "list", "path": ""},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["about.html", "platform.html"],
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), td)

    def test_builder_file_ops_blank_write_path_defaults_to_first_assigned_target(self):
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>ok</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            out = asyncio.run(plugin.execute(
                {"action": "write", "path": "", "content": valid_html},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["about.html", "platform.html"],
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), str(Path(td) / "about.html"))

    def test_builder_file_ops_blank_write_path_prefers_existing_assigned_file(self):
        plugin = FileOpsPlugin()
        valid_html = (
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head>"
            "<body><main><section>updated</section></main></body></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "platform.html"
            target.write_text("<html><body>draft</body></html>", encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {"action": "write", "path": "", "content": valid_html},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["about.html", "platform.html"],
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), str(target))

    def test_builder_file_ops_rejects_truncated_html_write(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.html"
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "<!DOCTYPE html><html><head><style>body{opacity:1}... [TRUNCATED]",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["index.html"],
                    "file_ops_can_write_root_index": True,
                },
            ))
            self.assertFalse(out.success)
            self.assertIn("truncation marker", str(out.error).lower())
            self.assertFalse(target.exists())

    def test_builder_file_ops_blank_read_path_defaults_to_existing_assigned_file(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "about.html"
            target.write_text("<html><body>about</body></html>", encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {"action": "read", "path": ""},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["about.html", "platform.html"],
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), str(target))
        self.assertIn("about", out.data.get("content", ""))

    def test_builder_file_ops_blank_read_path_falls_back_to_existing_index_without_targets(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.html"
            target.write_text("<html><body>index</body></html>", encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {"action": "read", "path": ""},
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                },
            ))
        self.assertTrue(out.success)
        self.assertEqual(out.data.get("path"), str(target))
        self.assertIn("index", out.data.get("content", ""))

    def test_builder_file_ops_rejects_large_regression_over_existing_valid_page(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "index.html"
            strong_html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Strong Page</title><style>
:root{--bg:#0b0c0f;--fg:#f5f5f7;--line:rgba(255,255,255,.12);}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:24px;border-bottom:1px solid var(--line)}
main{display:grid;gap:24px;padding:32px}section{padding:28px;border:1px solid var(--line);border-radius:24px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.cta{display:flex;gap:12px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style></head>
<body><header><nav><a href="index.html">Home</a><a href="about.html">About</a></nav></header>
<main><section><h1>Strong Existing Page</h1><p>This page already has real commercial content, layout, and polish.</p></section>
<section class="grid"><article><h2>Card A</h2><p>Dense copy.</p></article><article><h2>Card B</h2><p>Dense copy.</p></article><article><h2>Card C</h2><p>Dense copy.</p></article></section>
<section class="cta"><button>Explore</button><button>Contact</button></section></main><footer>Footer</footer><script>console.log('ok')</script></body></html>"""
            target.write_text(strong_html, encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "<!DOCTYPE html><html><head><meta charset='UTF-8'><style>body{margin:0}</style></head><body><h1>Stub</h1></body></html>",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["index.html"],
                    "file_ops_can_write_root_index": True,
                },
            ))
        self.assertFalse(out.success)
        self.assertIn("regression", str(out.error).lower())

    def test_polisher_file_ops_rejects_replay_placeholder_css_write(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "styles.css"
            target.write_text("body{margin:0;padding:0;display:block;}" * 120, encoding="utf-8")
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "<omitted large file content during replay>",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "polisher",
                    "file_ops_output_dir": td,
                },
            ))
        self.assertFalse(out.success)
        self.assertIn("placeholder", str(out.error).lower())

    def test_polisher_file_ops_rejects_shared_styles_regression(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "styles.css"
            target.write_text(
                (
                    ":root{--bg:#0b0c0f;--fg:#f5f5f7;}"
                    "body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui;}"
                    ".hero{min-height:100vh;display:grid;place-items:center;}"
                    ".nav{display:flex;gap:16px;padding:24px;}"
                ) * 80,
                encoding="utf-8",
            )
            out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(target),
                    "content": "body{margin:0;}",
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "polisher",
                    "file_ops_output_dir": td,
                },
            ))
        self.assertFalse(out.success)
        self.assertIn("shared asset", str(out.error).lower())

    def test_file_ops_write_postprocesses_remote_font_dependencies(self):
        plugin = FileOpsPlugin()
        with tempfile.TemporaryDirectory() as td:
            html_target = Path(td) / "index.html"
            css_target = Path(td) / "styles.css"
            html_out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(html_target),
                    "content": (
                        "<!DOCTYPE html><html><head>"
                        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
                        "<link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap\" rel=\"stylesheet\">"
                        "</head><body><main>ok</main></body></html>"
                    ),
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "builder",
                    "file_ops_output_dir": td,
                    "file_ops_allowed_html_targets": ["index.html"],
                    "file_ops_can_write_root_index": True,
                },
            ))
            css_out = asyncio.run(plugin.execute(
                {
                    "action": "write",
                    "path": str(css_target),
                    "content": (
                        "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap');\n"
                        "body{font-family:'Inter',sans-serif;}"
                    ),
                },
                context={
                    "allowed_dirs": [td],
                    "file_ops_node_type": "polisher",
                    "file_ops_output_dir": td,
                },
            ))

            self.assertTrue(html_out.success)
            self.assertTrue(css_out.success)
            self.assertNotIn("fonts.googleapis.com", html_target.read_text(encoding="utf-8"))
            self.assertNotIn("fonts.googleapis.com", css_target.read_text(encoding="utf-8"))


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

    def test_prepare_messages_omits_large_file_write_payloads_from_replay(self):
        html = "<!DOCTYPE html><html><body>" + ("A" * 6000) + "</body></html>"
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "build"},
            {
                "role": "assistant",
                "content": "writing file",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "file_ops",
                        "arguments": json.dumps({
                            "action": "write",
                            "path": "/tmp/evermind_output/pricing.html",
                            "content": html,
                        }),
                    },
                }],
            },
        ]

        prepared = self.bridge._prepare_messages_for_request(messages, "kimi-coding")
        replay_args = prepared[2]["tool_calls"][0]["function"]["arguments"]
        replay_payload = json.loads(replay_args)
        self.assertEqual(replay_payload.get("path"), "/tmp/evermind_output/pricing.html")
        self.assertEqual(replay_payload.get("content"), "")
        self.assertEqual(replay_payload.get("content_omitted"), True)
        self.assertGreater(replay_payload.get("content_chars", 0), 6000)
        self.assertNotIn("AAAA", replay_args)
        self.assertNotIn("<omitted large file content during replay>", replay_args)

    def test_builder_forced_text_messages_use_clean_two_message_context(self):
        forced = self.bridge._builder_forced_text_messages(
            "SYSTEM",
            "Build a premium multi-page site.",
            tool_results=[{"success": True, "data": {"path": "/tmp/evermind_output", "entries": [{"name": "index.html"}]}}],
            output_text="Partial draft only",
            force_text_reason="tool_research_loop",
        )
        self.assertEqual(len(forced), 2)
        self.assertEqual(forced[0]["role"], "system")
        self.assertEqual(forced[1]["role"], "user")
        self.assertIn("Forced final delivery reason", forced[1]["content"])
        self.assertIn("Workspace/tool summary", forced[1]["content"])
        self.assertNotIn("tool_calls", forced[1])

    def test_polisher_loop_guard_is_non_retryable_and_falls_back(self):
        error = "polisher loop guard triggered after 4 non-write tool iterations without any file write."
        self.assertFalse(self.bridge._should_retry_same_model(error))
        self.assertTrue(self.bridge._should_fallback_to_next_model(error))

    def test_openai_compatible_polisher_loop_guard_fails_fast_without_writes(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._polisher_force_write_threshold = MagicMock(return_value=2)

        class StubBrowserPlugin:
            name = "browser"
            description = "Browser tool"

            def get_tool_definition(self):
                return {
                    "name": "browser",
                    "description": self.description,
                    "parameters": {
                        "type": "object",
                        "properties": {"action": {"type": "string"}},
                        "required": ["action"],
                    },
                }

            async def execute(self, params, context=None):
                return PluginResult(
                    success=True,
                    data={"action": params.get("action"), "observation": "ok"},
                )

        def _tool_stream(call_index: int):
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=f"tool_{call_index}",
                                    function=SimpleNamespace(
                                        name="browser",
                                        arguments='{"action":"observe"}',
                                    ),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls > 2:
                    raise AssertionError("polisher loop guard should stop before a third tool-only response")
                return _tool_stream(self.calls)

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "polisher", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="Polish the existing luxury multi-page site under /tmp/evermind_output/",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[StubBrowserPlugin()],
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("polisher loop guard", result["error"])
        self.assertEqual(result.get("tool_call_stats", {}).get("browser"), 2)

    def test_openai_compatible_polisher_grants_one_write_after_forced_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text("<!doctype html><html><body>home</body></html>", encoding="utf-8")
            (out / "styles.css").write_text("body{color:#111;}", encoding="utf-8")
            (out / "app.js").write_text("console.log('ok');", encoding="utf-8")

            bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test", "output_dir": td})
            bridge._polisher_force_write_threshold = MagicMock(return_value=4)

            class StubFileOpsPlugin:
                name = "file_ops"
                description = "File operations"

                def get_tool_definition(self):
                    return {
                        "name": "file_ops",
                        "description": self.description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["action"],
                        },
                    }

                async def execute(self, params, context=None):
                    action = params.get("action")
                    if action == "list":
                        return PluginResult(
                            success=True,
                            data={
                                "path": td,
                                "entries": [
                                    {"name": "index.html"},
                                    {"name": "styles.css"},
                                    {"name": "app.js"},
                                ],
                            },
                        )
                    if action == "read":
                        return PluginResult(
                            success=True,
                            data={"path": params.get("path"), "content": "stub"},
                        )
                    if action == "write":
                        return PluginResult(
                            success=True,
                            data={"path": params.get("path"), "written": True},
                        )
                    return PluginResult(success=False, error="unsupported")

            def _tool_stream(call_index: int, args: Dict[str, Any]):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id=f"tool_{call_index}",
                                        function=SimpleNamespace(
                                            name="file_ops",
                                            arguments=json.dumps(args),
                                        ),
                                    )
                                ],
                            ),
                            finish_reason="tool_calls",
                        )
                    ],
                    usage=None,
                )

            def _final_stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content='{"status":"done"}',
                                tool_calls=None,
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

            class _DummyCompletions:
                def __init__(self):
                    self.calls = 0

                def create(self, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return _tool_stream(self.calls, {"action": "list", "path": td})
                    if self.calls == 2:
                        return _tool_stream(self.calls, {"action": "read", "path": str(out / "styles.css")})
                    if self.calls == 3:
                        return _tool_stream(self.calls, {"action": "read", "path": str(out / "app.js")})
                    if self.calls == 4:
                        return _tool_stream(self.calls, {"action": "read", "path": str(out / "index.html")})
                    if self.calls == 5:
                        return _tool_stream(
                            self.calls,
                            {
                                "action": "write",
                                "path": str(out / "styles.css"),
                                "content": "body{color:#222;}",
                            },
                        )
                    return _final_stream()

            class _DummyChat:
                def __init__(self):
                    self.completions = _DummyCompletions()

            class _DummyOpenAI:
                def __init__(self, *args, **kwargs):
                    self.chat = _DummyChat()

            model_info = bridge._resolve_model("kimi-coding")
            node = {"type": "polisher", "model": "kimi-coding"}

            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="Polish this premium three-page site without collapsing routes.",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[StubFileOpsPlugin()],
                    )
                )

        self.assertTrue(result["success"])
        self.assertEqual(result.get("tool_call_stats", {}).get("file_ops"), 5)
        self.assertEqual(result.get("error", ""), "")

    def test_openai_compatible_polisher_replays_all_tool_results_before_followup_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text("<!doctype html><html><body>home</body></html>", encoding="utf-8")
            (out / "styles.css").write_text("body{color:#111;}", encoding="utf-8")
            (out / "app.js").write_text("console.log('ok');", encoding="utf-8")

            bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test", "output_dir": td})
            bridge._polisher_force_write_threshold = MagicMock(return_value=10)

            class StubFileOpsPlugin:
                name = "file_ops"
                description = "File operations"

                def get_tool_definition(self):
                    return {
                        "name": "file_ops",
                        "description": self.description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["action"],
                        },
                    }

                async def execute(self, params, context=None):
                    action = params.get("action")
                    if action == "list":
                        return PluginResult(
                            success=True,
                            data={
                                "path": td,
                                "entries": [
                                    {"name": "index.html"},
                                    {"name": "styles.css"},
                                    {"name": "app.js"},
                                ],
                            },
                        )
                    if action == "read":
                        return PluginResult(
                            success=True,
                            data={"path": params.get("path"), "content": "stub"},
                        )
                    return PluginResult(success=False, error="unsupported")

            def _multi_tool_stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_1",
                                        function=SimpleNamespace(
                                            name="file_ops",
                                            arguments=json.dumps({"action": "list", "path": td}),
                                        ),
                                    ),
                                    SimpleNamespace(
                                        index=1,
                                        id="call_2",
                                        function=SimpleNamespace(
                                            name="file_ops",
                                            arguments=json.dumps({"action": "read", "path": str(out / "styles.css")}),
                                        ),
                                    ),
                                    SimpleNamespace(
                                        index=2,
                                        id="call_3",
                                        function=SimpleNamespace(
                                            name="file_ops",
                                            arguments=json.dumps({"action": "read", "path": str(out / "app.js")}),
                                        ),
                                    ),
                                ],
                            ),
                            finish_reason="tool_calls",
                        )
                    ],
                    usage=None,
                )

            def _final_stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content='{"status":"done"}',
                                tool_calls=None,
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

            captured_batches = []

            class _DummyCompletions:
                def __init__(self):
                    self.calls = 0

                def create(self, **kwargs):
                    captured_batches.append(json.loads(json.dumps(kwargs.get("messages", []))))
                    self.calls += 1
                    if self.calls == 1:
                        return _multi_tool_stream()
                    return _final_stream()

            class _DummyChat:
                def __init__(self):
                    self.completions = _DummyCompletions()

            class _DummyOpenAI:
                def __init__(self, *args, **kwargs):
                    self.chat = _DummyChat()

            model_info = bridge._resolve_model("kimi-coding")
            node = {"type": "polisher", "model": "kimi-coding"}

            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="Polish this premium multi-page site without collapsing routes.",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[StubFileOpsPlugin()],
                    )
                )

        self.assertTrue(result["success"])
        self.assertEqual(len(captured_batches), 2)
        replay_messages = captured_batches[1]
        assistant_index = next(
            i for i, msg in enumerate(replay_messages)
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        )
        self.assertEqual(
            [msg.get("role") for msg in replay_messages[assistant_index + 1:assistant_index + 5]],
            ["tool", "tool", "tool", "user"],
        )
        self.assertEqual(
            [replay_messages[assistant_index + offset].get("tool_call_id") for offset in (1, 2, 3)],
            ["call_1", "call_2", "call_3"],
        )
        self.assertIn(
            "VERY NEXT response must contain one or more file_ops write calls",
            replay_messages[assistant_index + 4].get("content", ""),
        )

    def test_openai_compatible_builder_timeout_falls_back_to_forced_text(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        plugin = FileOpsPlugin()

        def _slow_stream():
            time.sleep(0.05)
            if False:
                yield None

        def _final_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="```html index.html\n<!DOCTYPE html><html><body>Luxury</body></html>\n```",
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return _slow_stream()
                return _final_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch.dict("os.environ", {"EVERMIND_BUILDER_FORCED_TEXT_TIMEOUT_SEC": "1"}, clear=False):
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="做一个 8 页奢侈品官网，并保存为命名 HTML 页面",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[plugin],
                    )
                )

        self.assertTrue(result["success"])
        self.assertIn("<!DOCTYPE html>", result["output"])
        self.assertEqual(result["mode"], "openai_compatible_forced_text_timeout")

    def test_openai_compatible_builder_timeout_auto_save_counts_file_ops(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=0.02)
        plugin = FileOpsPlugin()

        large_body = "<section>" + ("Arena combat " * 220) + "</section>"
        streamed_html = (
            "```html index.html\n"
            "<!DOCTYPE html><html><head><title>Arena</title></head>"
            f"<body><main><h1>Arena</h1>{large_body}<canvas id=\"game\"></canvas></main></body></html>\n```"
        )

        def _hanging_stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=streamed_html,
                            tool_calls=None,
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(0.06)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _hanging_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="做一个可玩的竞技场网页游戏，并保存为 index.html",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[plugin],
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "openai_compatible_text_mode_auto_save")
        self.assertGreaterEqual(int(result["tool_call_stats"].get("file_ops", 0) or 0), 1)
        self.assertTrue(any(item.get("success") for item in result["tool_results"]))

    def test_openai_compatible_builder_active_text_stream_survives_prewrite_deadline(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._builder_prewrite_call_timeout = MagicMock(return_value=0.01)
        bridge._timeout_for_node = MagicMock(return_value=1)
        plugin = FileOpsPlugin()

        body_chunk = "<section>" + ("Voxel arena " * 80) + "</section>"
        final_html = (
            "```html index.html\n"
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"><title>Voxel</title></head>"
            "<body><main><h1>Voxel Strike</h1>"
            f"{body_chunk}"
            "<canvas id=\"game\"></canvas><button>Start</button></main></body></html>\n```"
        )
        split_at = 380

        def _stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=final_html[:split_at],
                            tool_calls=None,
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            time.sleep(0.05)
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=final_html[split_at:],
                            tool_calls=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        class _DummyCompletions:
            def create(self, **kwargs):
                return _stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "builder", "model": "kimi-coding"}

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge.config["output_dir"] = tmpdir
            with patch("openai.OpenAI", _DummyOpenAI):
                result = asyncio.run(
                    bridge._execute_openai_compatible(
                        node=node,
                        input_data="创建一个我的世界风格 3D 像素射击游戏单页。",
                        model_info=model_info,
                        on_progress=None,
                        plugins=[plugin],
                    )
                )

            saved_path = Path(tmpdir) / "index.html"
            self.assertTrue(result["success"])
            self.assertEqual(result["mode"], "openai_compatible")
            self.assertTrue(saved_path.exists())
            self.assertIn("Voxel Strike", saved_path.read_text(encoding="utf-8"))

    def test_openai_compatible_non_builder_timeout_uses_node_specific_error(self):
        bridge = AIBridge(config={"kimi_api_key": "sk-kimi-test"})
        bridge._timeout_for_node = MagicMock(return_value=0.01)

        def _slow_stream():
            time.sleep(0.05)
            if False:
                yield None

        class _DummyCompletions:
            def create(self, **kwargs):
                return _slow_stream()

        class _DummyChat:
            def __init__(self):
                self.completions = _DummyCompletions()

        class _DummyOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = _DummyChat()

        model_info = bridge._resolve_model("kimi-coding")
        node = {"type": "analyst", "model": "kimi-coding"}

        with patch("openai.OpenAI", _DummyOpenAI):
            result = asyncio.run(
                bridge._execute_openai_compatible(
                    node=node,
                    input_data="Research cinematic travel website references.",
                    model_info=model_info,
                    on_progress=None,
                    plugins=[],
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("analyst hard-ceiling timeout", result["error"])
        self.assertNotIn("builder", result["error"].lower())
        self.assertNotIn("no file write produced", result["error"].lower())

    def test_sanitize_error_has_non_empty_fallback(self):
        self.assertEqual(_sanitize_error(""), "Unknown error")
        self.assertEqual(_sanitize_error(None), "Unknown error")


if __name__ == "__main__":
    unittest.main()
