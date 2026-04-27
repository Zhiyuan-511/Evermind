"""V6.1: Kimi key-aware endpoint routing tests."""

import pytest

from ai_bridge import AIBridge, MODEL_REGISTRY


@pytest.fixture(autouse=True)
def _isolate_kimi_env(monkeypatch):
    """Prevent leakage from other tests that set KIMI_API_KEY or class-level
    key-pool cursors/cooldowns. We want each test to see ONLY the config
    passed into AIBridge, so the routing decision depends solely on the key
    prefix we inject."""
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY_2", raising=False)
    monkeypatch.delenv("EVERMIND_KIMI_FALLBACK_API_BASE", raising=False)
    AIBridge._provider_key_cooldowns.pop("kimi", None)
    AIBridge._provider_key_cursor.pop("kimi", None)
    yield


def _bridge_with_key(key: str) -> AIBridge:
    return AIBridge(config={"kimi_api_key": key})


def test_is_coding_plan_key_sk_kimi_prefix():
    assert AIBridge._is_kimi_coding_plan_key("sk-kimi-abc123") is True
    assert AIBridge._is_kimi_coding_plan_key("sk-kimi-") is True


def test_is_coding_plan_key_platform_prefix():
    assert AIBridge._is_kimi_coding_plan_key("sk-abc123") is False
    assert AIBridge._is_kimi_coding_plan_key("") is False
    assert AIBridge._is_kimi_coding_plan_key(None) is False  # type: ignore[arg-type]


def test_coding_key_routes_to_coding_endpoint():
    bridge = _bridge_with_key("sk-kimi-test123")
    info = dict(MODEL_REGISTRY["kimi-coding"])
    adjusted = bridge._kimi_routing_adjusted(info)
    assert adjusted["api_base"] == "https://api.kimi.com/coding/v1"
    assert adjusted["fallback_api_bases"] == [
        "https://api.moonshot.ai/v1",
        "https://api.moonshot.cn/v1",
    ]


def test_platform_key_keeps_moonshot_cn_primary():
    bridge = _bridge_with_key("sk-legacykey")
    info = dict(MODEL_REGISTRY["kimi-coding"])
    adjusted = bridge._kimi_routing_adjusted(info)
    assert adjusted["api_base"] == "https://api.moonshot.cn/v1"
    assert "https://api.kimi.com/coding/v1" in adjusted["fallback_api_bases"]


def test_non_kimi_provider_untouched():
    bridge = _bridge_with_key("sk-kimi-xxx")
    info = {"provider": "openai", "api_base": "https://api.openai.com/v1"}
    adjusted = bridge._kimi_routing_adjusted(info)
    assert adjusted is info  # no copy, no mutation


def test_registry_is_not_mutated():
    """Critical: adjusting one call must not pollute the global registry
    for other AIBridge instances with different keys."""
    original_base = MODEL_REGISTRY["kimi-coding"]["api_base"]
    original_fb = list(MODEL_REGISTRY["kimi-coding"]["fallback_api_bases"])

    bridge_coding = _bridge_with_key("sk-kimi-aaa")
    bridge_coding._kimi_routing_adjusted(MODEL_REGISTRY["kimi-coding"])
    assert MODEL_REGISTRY["kimi-coding"]["api_base"] == original_base
    assert MODEL_REGISTRY["kimi-coding"]["fallback_api_bases"] == original_fb


def test_resolved_api_base_uses_coding_primary_for_coding_key():
    bridge = _bridge_with_key("sk-kimi-aaa")
    info = MODEL_REGISTRY["kimi-coding"]
    base = bridge._resolved_api_base_for_model_info(info)
    assert base == "https://api.kimi.com/coding/v1"


def test_fallback_bases_reordered_for_coding_key():
    bridge = _bridge_with_key("sk-kimi-aaa")
    info = MODEL_REGISTRY["kimi-coding"]
    fallbacks = bridge._fallback_gateway_bases(info)
    assert fallbacks[0] == "https://api.moonshot.ai/v1"
    assert "https://api.moonshot.cn/v1" in fallbacks
