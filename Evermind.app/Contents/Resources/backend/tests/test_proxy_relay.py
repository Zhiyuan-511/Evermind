"""
Evermind Backend — Proxy/Relay Tests
Covers circuit-breaker, _safe_cost, retry scoping, and RelayManager CRUD.
"""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from proxy_relay import (
    RelayEndpoint,
    RelayManager,
    _safe_cost,
    relay_pool_model_id,
    relay_template_catalog,
    resolve_relay_template,
)


class TestSafeCost(unittest.TestCase):
    def test_returns_float_on_success(self):
        mock_resp = MagicMock()
        with patch.dict("sys.modules", {"litellm": MagicMock(completion_cost=MagicMock(return_value=0.0042))}):
            result = _safe_cost(mock_resp, "openai/gpt-4o")
            self.assertAlmostEqual(result, 0.0042)

    def test_returns_zero_on_exception(self):
        mock_resp = MagicMock()
        result = _safe_cost(mock_resp, "bad/model")
        self.assertEqual(result, 0.0)


class TestRelayEndpointCircuitBreaker(unittest.TestCase):
    def _make_endpoint(self) -> RelayEndpoint:
        return RelayEndpoint(
            id="test-1", name="Test", base_url="http://example.com",
            api_key="key", models=["gpt-4o"],
        )

    def test_circuit_closed_initially(self):
        ep = self._make_endpoint()
        self.assertFalse(ep.circuit_open)

    def test_circuit_opens_after_threshold_failures(self):
        ep = self._make_endpoint()
        for _ in range(RelayEndpoint.CIRCUIT_FAILURE_THRESHOLD):
            ep._record_failure()
        self.assertTrue(ep.circuit_open)

    def test_circuit_closes_on_success(self):
        ep = self._make_endpoint()
        for _ in range(RelayEndpoint.CIRCUIT_FAILURE_THRESHOLD):
            ep._record_failure()
        self.assertTrue(ep.circuit_open)
        ep._record_success()
        self.assertFalse(ep.circuit_open)

    def test_circuit_closes_after_recovery_period(self):
        ep = self._make_endpoint()
        for _ in range(RelayEndpoint.CIRCUIT_FAILURE_THRESHOLD):
            ep._record_failure()
        # Simulate time passing beyond recovery
        ep._circuit_open_until = time.time() - 1
        self.assertFalse(ep.circuit_open)


class TestRelayManagerCRUD(unittest.TestCase):
    def test_add_and_list(self):
        mgr = RelayManager()
        ep = mgr.add(name="R1", base_url="http://example.com/v1", api_key="k")
        listed = mgr.list()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "R1")
        # API key should be masked in list output
        self.assertNotEqual(listed[0].get("api_key"), "k")

    def test_remove(self):
        mgr = RelayManager()
        ep = mgr.add(name="R1", base_url="http://example.com/v1")
        self.assertTrue(mgr.remove(ep.id))
        self.assertEqual(len(mgr.list()), 0)

    def test_remove_nonexistent(self):
        mgr = RelayManager()
        self.assertFalse(mgr.remove("does-not-exist"))

    def test_load_exports_roundtrip(self):
        mgr = RelayManager()
        mgr.add(
            name="A",
            base_url="http://a.com",
            api_key="key-a",
            models=["m1"],
            provider="google",
            api_style="litellm_proxy",
            model_map={"m1": "gemini/gemini-2.5-pro"},
        )
        mgr.add(name="B", base_url="http://b.com", api_key="key-b", models=["m2"])
        exported = mgr.export()

        mgr2 = RelayManager()
        mgr2.load(exported)
        self.assertEqual(len(mgr2.list()), 2)
        # Verify full-fidelity keys survive roundtrip
        configs = mgr2.export()
        keys = {c["name"]: c["api_key"] for c in configs}
        self.assertEqual(keys["A"], "key-a")
        self.assertEqual(keys["B"], "key-b")
        cfg_a = next(item for item in configs if item["name"] == "A")
        self.assertEqual(cfg_a["provider"], "google")
        self.assertEqual(cfg_a["api_style"], "litellm_proxy")
        self.assertEqual(cfg_a["model_map"], {"m1": "gemini/gemini-2.5-pro"})

    def test_get_all_models_skips_disabled(self):
        mgr = RelayManager()
        ep = mgr.add(name="R1", base_url="http://a.com", models=["m1"])
        ep.enabled = False
        self.assertEqual(len(mgr.get_all_models()), 0)
        ep.enabled = True
        self.assertGreater(len(mgr.get_all_models()), 0)

    def test_empty_models_default(self):
        mgr = RelayManager()
        ep = mgr.add(name="R1", base_url="http://a.com")
        # When no models specified, should have defaults
        self.assertGreater(len(ep.models), 0)

    def test_gemini_template_supplies_default_base_and_model_map(self):
        mgr = RelayManager()
        ep = mgr.add(
            name="Gemini Compat",
            base_url="",
            api_key="key",
            provider="google",
            api_style="openai_compatible",
            template_id="gemini_openai",
            models=[],
            model_map={},
        )

        self.assertEqual(ep.template_id, "gemini_openai")
        self.assertEqual(ep.base_url, "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertIn("gemini-2.5-pro", ep.models)
        self.assertEqual(ep.model_map["gemini-2.5-pro"], "gemini-2.5-pro")

    def test_openai_template_supplies_gpt54_defaults(self):
        mgr = RelayManager()
        ep = mgr.add(
            name="OpenAI Compat",
            base_url="",
            api_key="key",
            provider="openai",
            api_style="openai_compatible",
            template_id="openai_compat",
            models=[],
            model_map={},
        )

        self.assertEqual(ep.template_id, "openai_compat")
        self.assertEqual(ep.base_url, "https://api.openai.com/v1")
        self.assertIn("gpt-5.4", ep.models)
        self.assertEqual(ep.model_map["gpt-5.4"], "gpt-5.4")


class TestRelayTemplates(unittest.TestCase):
    def test_catalog_exposes_key_provider_templates(self):
        catalog = relay_template_catalog()
        ids = {item["id"] for item in catalog}

        self.assertIn("openai_compat", ids)
        self.assertIn("litellm_proxy", ids)
        self.assertIn("gemini_openai", ids)
        self.assertIn("glm_openai", ids)
        self.assertIn("minimax_openai", ids)
        self.assertIn("claude_openai_compat", ids)

    def test_resolve_relay_template_matches_provider_style_and_base(self):
        template = resolve_relay_template(
            provider="zhipu",
            api_style="openai_compatible",
            base_url="https://open.bigmodel.cn/api/paas/v4/",
        )

        self.assertEqual(template["id"], "glm_openai")

    def test_claude_openai_template_uses_current_official_model_ids(self):
        template = resolve_relay_template(
            provider="anthropic",
            api_style="openai_compatible",
            base_url="https://api.anthropic.com/v1/",
        )

        self.assertEqual(template["id"], "claude_openai_compat")
        self.assertEqual(template["default_model_map"]["claude-4-sonnet"], "claude-sonnet-4-6")
        self.assertEqual(template["default_model_map"]["claude-4-opus"], "claude-opus-4-6")

    def test_openai_template_matches_official_base(self):
        template = resolve_relay_template(
            provider="openai",
            api_style="openai_compatible",
            base_url="https://api.openai.com/v1",
        )

        self.assertEqual(template["id"], "openai_compat")
        self.assertIn("gpt-5.4", template["default_models"])


class TestRelayPoolRouting(unittest.TestCase):
    def test_get_all_models_includes_pool_alias_for_shared_model(self):
        mgr = RelayManager()
        mgr.load([
            {
                "id": "relay_a",
                "name": "Relay A",
                "base_url": "https://relay-a.example/v1",
                "api_key": "key-a",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 240},
            },
            {
                "id": "relay_b",
                "name": "Relay B",
                "base_url": "https://relay-b.example/v1",
                "api_key": "key-b",
                "models": ["gpt-5.4", "claude-4-sonnet"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 110},
            },
        ])

        models = mgr.get_all_models()
        pool_id = relay_pool_model_id("gpt-5.4")

        self.assertIn(pool_id, models)
        self.assertEqual(models[pool_id]["relay_strategy"], "pool")
        self.assertEqual(models[pool_id]["relay_pool_endpoints"], ["relay_b", "relay_a"])


class TestRelayProviderKeyFallback(unittest.TestCase):
    def test_call_reuses_saved_provider_env_key_when_endpoint_key_empty(self):
        mgr = RelayManager()
        endpoint = mgr.add(
            name="OpenAI Relay",
            base_url="https://relay.example/v1",
            api_key="",
            provider="openai",
            api_style="openai_compatible",
            models=["gpt-5.4"],
        )
        captured_kwargs = {}

        async def fake_acompletion(**kwargs):
            captured_kwargs.update(kwargs)
            if kwargs.get("stream"):
                async def gen():
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"), finish_reason="stop")],
                        model="gpt-5.4",
                        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=1, total_tokens=1),
                    )
                return gen()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                model="gpt-5.4",
                usage={},
            )

        litellm_mock = SimpleNamespace(completion=MagicMock(), acompletion=fake_acompletion)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env-openai"}, clear=False), \
             patch.dict("sys.modules", {"litellm": litellm_mock}):
            result = asyncio.run(
                mgr.call(
                    endpoint_id=endpoint.id,
                    model="gpt-5.4",
                    messages=[{"role": "user", "content": "hi"}],
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(captured_kwargs["api_key"], "sk-env-openai")

    def test_ranked_endpoints_for_model_prefers_health_then_latency(self):
        mgr = RelayManager()
        mgr.load([
            {
                "id": "relay_fast",
                "name": "Fast Relay",
                "base_url": "https://relay-fast.example/v1",
                "api_key": "key-fast",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 90},
            },
            {
                "id": "relay_slow",
                "name": "Slow Relay",
                "base_url": "https://relay-slow.example/v1",
                "api_key": "key-slow",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 420},
            },
            {
                "id": "relay_open",
                "name": "Open Circuit Relay",
                "base_url": "https://relay-open.example/v1",
                "api_key": "key-open",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 40},
            },
        ])
        open_ep = mgr.get("relay_open")
        for _ in range(RelayEndpoint.CIRCUIT_FAILURE_THRESHOLD):
            open_ep._record_failure()

        ranked = mgr.ranked_endpoints_for_model("gpt-5.4")
        self.assertEqual([ep.id for ep in ranked], ["relay_fast", "relay_slow", "relay_open"])

    def test_ranked_endpoints_prefer_builder_profile_compatible_relays(self):
        mgr = RelayManager()
        mgr.load([
            {
                "id": "relay_full",
                "name": "Full Relay",
                "base_url": "https://relay-full.example/v1",
                "api_key": "key-full",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {
                    "success": True,
                    "builder_profile_ok": True,
                    "latency_ms": 180,
                },
            },
            {
                "id": "relay_basic",
                "name": "Basic Relay",
                "base_url": "https://relay-basic.example/v1",
                "api_key": "key-basic",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {
                    "success": False,
                    "connectivity_ok": True,
                    "builder_profile_ok": False,
                    "latency_ms": 80,
                },
            },
        ])

        ranked = mgr.ranked_endpoints_for_model("gpt-5.4")
        self.assertEqual([ep.id for ep in ranked], ["relay_full", "relay_basic"])

    def test_call_best_fails_over_to_next_endpoint(self):
        mgr = RelayManager()
        mgr.load([
            {
                "id": "relay_a",
                "name": "Relay A",
                "base_url": "https://relay-a.example/v1",
                "api_key": "key-a",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 120},
            },
            {
                "id": "relay_b",
                "name": "Relay B",
                "base_url": "https://relay-b.example/v1",
                "api_key": "key-b",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 220},
            },
        ])
        mgr.call = AsyncMock(side_effect=[
            {"success": False, "error": "502 bad gateway", "relay": "Relay A"},
            {"success": True, "content": "ok", "model": "gpt-5.4", "relay": "Relay B", "usage": {}, "cost": 0.0},
        ])

        result = asyncio.run(mgr.call_best("gpt-5.4", [{"role": "user", "content": "hi"}]))

        self.assertTrue(result["success"])
        self.assertEqual(result["relay"], "Relay B")
        self.assertEqual(result["relay_pool_tried"], ["Relay A", "Relay B"])
        self.assertTrue(result["relay_pool_failover"])
        self.assertEqual(mgr.call.await_count, 2)
        self.assertEqual(mgr.call.await_args_list[0].kwargs["endpoint_id"], "relay_a")
        self.assertEqual(mgr.call.await_args_list[1].kwargs["endpoint_id"], "relay_b")
        self.assertTrue(mgr.call.await_args_list[0].kwargs["_pool_mode"])

    def test_call_best_stops_on_non_failover_request_error(self):
        mgr = RelayManager()
        mgr.load([
            {
                "id": "relay_a",
                "name": "Relay A",
                "base_url": "https://relay-a.example/v1",
                "api_key": "key-a",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 120},
            },
            {
                "id": "relay_b",
                "name": "Relay B",
                "base_url": "https://relay-b.example/v1",
                "api_key": "key-b",
                "models": ["gpt-5.4"],
                "enabled": True,
                "last_test": {"success": True, "latency_ms": 220},
            },
        ])
        mgr.call = AsyncMock(return_value={
            "success": False,
            "error": "invalid_request_error: maximum context length exceeded",
            "relay": "Relay A",
        })

        result = asyncio.run(mgr.call_best("gpt-5.4", [{"role": "user", "content": "hi"}]))

        self.assertFalse(result["success"])
        self.assertEqual(result["relay_pool_tried"], ["Relay A"])
        self.assertEqual(mgr.call.await_count, 1)

    def test_model_registry_entries_use_exact_mapped_model_for_litellm_proxy(self):
        endpoint = RelayEndpoint(
            id="relay_proxy",
            name="Proxy Relay",
            base_url="https://proxy.example/v1",
            api_key="key",
            models=["claude-4-sonnet"],
            provider="anthropic",
            api_style="litellm_proxy",
            model_map={"claude-4-sonnet": "anthropic/claude-sonnet-4-5"},
        )

        entries = endpoint.to_model_registry_entries()
        info = entries["relay/relay_proxy/claude-4-sonnet"]
        self.assertEqual(info["litellm_id"], "anthropic/claude-sonnet-4-5")
        self.assertEqual(info["relay_model_name"], "claude-4-sonnet")
        self.assertEqual(info["relay_target_model"], "anthropic/claude-sonnet-4-5")

    def test_model_registry_entries_use_openai_prefix_for_openai_compatible_relays(self):
        endpoint = RelayEndpoint(
            id="relay_oa",
            name="OpenAI Compat Relay",
            base_url="https://compat.example/v1",
            api_key="key",
            models=["glm-4.5"],
            provider="zhipu",
            api_style="openai_compatible",
            model_map={"glm-4.5": "glm-4.5"},
        )

        entries = endpoint.to_model_registry_entries()
        info = entries["relay/relay_oa/glm-4.5"]
        self.assertEqual(info["litellm_id"], "openai/glm-4.5")

    def test_call_uses_exact_mapped_model_for_litellm_proxy(self):
        mgr = RelayManager()
        endpoint = mgr.add(
            name="Proxy Relay",
            base_url="https://proxy.example/v1",
            api_key="key",
            models=["gemini-2.5-pro"],
            provider="google",
            api_style="litellm_proxy",
            model_map={"gemini-2.5-pro": "gemini/gemini-2.5-pro"},
        )
        captured = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            if kwargs.get("stream"):
                async def gen():
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="OK"), finish_reason="stop")],
                        model="gemini/gemini-2.5-pro",
                        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=1, total_tokens=1),
                    )
                return gen()
            return SimpleNamespace(
                model="gemini/gemini-2.5-pro",
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
                usage=None,
            )

        litellm_mock = MagicMock(completion=MagicMock(), acompletion=fake_acompletion)
        with patch.dict("sys.modules", {"litellm": litellm_mock}):
            result = asyncio.run(
                mgr.call(
                    endpoint_id=endpoint.id,
                    model="gemini-2.5-pro",
                    messages=[{"role": "user", "content": "hi"}],
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(captured["model"], "gemini/gemini-2.5-pro")
        self.assertEqual(result["resolved_model"], "gemini/gemini-2.5-pro")


class TestRelayCompatibilityProbe(unittest.TestCase):
    def test_relay_test_requires_streaming_and_tool_calling_for_success(self):
        mgr = RelayManager()
        endpoint = mgr.add(
            name="Relay A",
            base_url="https://relay-a.example/v1",
            api_key="key-a",
            models=["gpt-5.4"],
        )

        def fake_completion(**kwargs):
            if kwargs.get("stream"):
                return [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="OK"), finish_reason=None)]
                    ),
                    SimpleNamespace(choices=[]),
                ]
            if kwargs.get("tools"):
                return SimpleNamespace(
                    model="gpt-5.4",
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        function=SimpleNamespace(name="ping", arguments='{"message":"pong"}')
                                    )
                                ],
                            )
                        )
                    ],
                )
            return SimpleNamespace(
                model="gpt-5.4",
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
            )

        with patch.dict("sys.modules", {"litellm": MagicMock(completion=fake_completion)}):
            result = asyncio.run(mgr.test(endpoint.id))

        self.assertTrue(result["success"])
        self.assertTrue(result["connectivity_ok"])
        self.assertTrue(result["streaming_ok"])
        self.assertTrue(result["tool_calling_ok"])
        self.assertTrue(result["builder_profile_ok"])
        self.assertTrue(endpoint.last_test["builder_profile_ok"])

    def test_relay_test_marks_basic_only_endpoint_as_incompatible_for_builder_profile(self):
        mgr = RelayManager()
        endpoint = mgr.add(
            name="Relay A",
            base_url="https://relay-a.example/v1",
            api_key="key-a",
            models=["gpt-5.4"],
        )

        def fake_completion(**kwargs):
            if kwargs.get("stream"):
                return [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="OK"), finish_reason=None)]
                    ),
                    SimpleNamespace(choices=[]),
                ]
            if kwargs.get("tools"):
                return SimpleNamespace(
                    model="gpt-5.4",
                    choices=[SimpleNamespace(message=SimpleNamespace(content="tool-less reply", tool_calls=[]))],
                )
            return SimpleNamespace(
                model="gpt-5.4",
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
            )

        with patch.dict("sys.modules", {"litellm": MagicMock(completion=fake_completion)}):
            result = asyncio.run(mgr.test(endpoint.id))

        self.assertFalse(result["success"])
        self.assertTrue(result["connectivity_ok"])
        self.assertTrue(result["streaming_ok"])
        self.assertFalse(result["tool_calling_ok"])
        self.assertFalse(result["builder_profile_ok"])
        self.assertIn("tool-calling probe", result["error"])


if __name__ == "__main__":
    unittest.main()
