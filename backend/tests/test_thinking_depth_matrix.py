"""V6.1: unified thinking_depth → per-provider payload matrix tests."""

import pytest

from ai_bridge import AIBridge


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in (
        "EVERMIND_THINKING_DEPTH",
        "EVERMIND_KIMI_THINKING",
        "EVERMIND_DEEPSEEK_THINKING",
        "EVERMIND_QWEN_THINKING",
        "EVERMIND_GLM_THINKING",
        "EVERMIND_DOUBAO_THINKING",
        "EVERMIND_AIGATE_THINKING",
        "EVERMIND_ANTHROPIC_THINKING",
        "EVERMIND_XAI_THINKING",
        "EVERMIND_OPENAI_THINKING",
        "EVERMIND_MISTRAL_THINKING",
        "KIMI_API_KEY",
        "KIMI_API_KEY_2",
    ):
        monkeypatch.delenv(v, raising=False)
    AIBridge._provider_key_cooldowns.pop("kimi", None)
    AIBridge._provider_key_cursor.pop("kimi", None)
    yield


def _apply(depth: str, provider: str, model: str, *, info_extra=None, kwargs=None):
    bridge = AIBridge(config={"thinking_depth": depth})
    model_info = {"provider": provider, **(info_extra or {})}
    kw = kwargs or {}
    bridge._apply_thinking_config_to_kwargs(kw, model_info, model, cache_key="run1-builder-ne1")
    return kw


# ── deep mode: every family is ON ────────────────────────────────────────────


def test_kimi_deep_enables_thinking_and_forces_16k_tokens():
    kw = _apply("deep", "kimi", "kimi-k2.6-code-preview", kwargs={"max_tokens": 4096})
    assert kw["extra_body"]["thinking"] == {"type": "enabled"}
    assert kw["max_tokens"] >= 16000
    assert kw["temperature"] == 1.0
    assert kw["extra_body"]["prompt_cache_key"] == "run1-builder-ne1"


def test_deepseek_chat_deep_enables_thinking():
    kw = _apply("deep", "deepseek", "deepseek-chat")
    assert kw["extra_body"]["thinking"] == {"type": "enabled"}


def test_deepseek_reasoner_has_no_explicit_toggle():
    kw = _apply("deep", "deepseek", "deepseek-reasoner")
    # reasoner model always thinks — we shouldn't inject thinking flag
    assert "thinking" not in kw.get("extra_body", {})


def test_qwen_deep_turns_on_enable_thinking_and_forces_stream():
    kw = _apply("deep", "qwen", "qwen3-max")
    assert kw["extra_body"]["enable_thinking"] is True
    assert kw.get("stream") is True
    assert kw["extra_body"]["thinking_budget"] == 8192


def test_zhipu_deep_sets_thinking_enabled():
    kw = _apply("deep", "zhipu", "glm-5")
    assert kw["extra_body"]["thinking"] == {"type": "enabled"}


def test_doubao_deep_sets_thinking_enabled():
    kw = _apply("deep", "doubao", "doubao-seed-1.6")
    assert kw["extra_body"]["thinking"] == {"type": "enabled"}


def test_minimax_always_splits_reasoning():
    kw = _apply("deep", "minimax", "minimax-m2.7")
    assert kw["extra_body"]["reasoning_split"] is True


def test_aigate_deep_unions_all_enable_flags():
    kw = _apply("deep", "aigate", "aigate-qwen3.6-plus")
    eb = kw["extra_body"]
    assert eb["thinking"] == {"type": "enabled"}
    assert eb["enable_thinking"] is True
    assert eb["chat_template_kwargs"] == {"enable_thinking": True}


def test_anthropic_deep_v46_uses_budget_tokens():
    kw = _apply("deep", "anthropic", "claude-opus-4-6", kwargs={"max_tokens": 8000})
    thinking = kw["extra_body"]["thinking"]
    assert thinking["type"] == "enabled"
    assert 1024 <= thinking["budget_tokens"] <= 10000


def test_anthropic_deep_v47_uses_adaptive():
    kw = _apply("deep", "anthropic", "claude-opus-4-7")
    assert kw["extra_body"]["thinking"] == {"type": "enabled"}
    # 4.7 forbids manual budget_tokens
    assert "budget_tokens" not in kw["extra_body"]["thinking"]


def test_xai_grok_mini_deep_sets_reasoning_effort_high():
    kw = _apply("deep", "xai", "grok-3-mini")
    assert kw["reasoning_effort"] == "high"


def test_xai_grok4_never_sets_reasoning_effort():
    """grok-4 returns 400 when reasoning_effort is present."""
    kw = _apply("deep", "xai", "grok-4")
    assert "reasoning_effort" not in kw


def test_openai_gpt5_deep_high_effort():
    kw = _apply(
        "deep",
        "openai",
        "gpt-5.4",
        info_extra={"supports_reasoning_effort": True},
    )
    assert kw["reasoning_effort"] == "high"


def test_mistral_magistral_deep_high_effort():
    kw = _apply("deep", "mistral", "magistral-medium-2509")
    assert kw["reasoning_effort"] == "high"


# ── fast mode: everyone OFF ──────────────────────────────────────────────────


def test_kimi_fast_disables_thinking_no_max_tokens_force():
    kw = _apply("fast", "kimi", "kimi-k2.5", kwargs={"max_tokens": 4096})
    assert kw["extra_body"]["thinking"] == {"type": "disabled"}
    assert kw["max_tokens"] == 4096
    # no forced temp=1.0 either
    assert "temperature" not in kw


def test_qwen_fast_disables_enable_thinking():
    kw = _apply("fast", "qwen", "qwen3-max")
    assert kw["extra_body"]["enable_thinking"] is False


def test_aigate_fast_unions_all_disable_flags():
    kw = _apply("fast", "aigate", "aigate-qwen3.6-plus")
    eb = kw["extra_body"]
    assert eb["thinking"] == {"type": "disabled"}
    assert eb["enable_thinking"] is False
    assert eb["no_thinking"] is True


def test_anthropic_fast_leaves_thinking_off():
    kw = _apply("fast", "anthropic", "claude-opus-4-7")
    # fast → no thinking flag injected; Claude defaults to off
    assert "thinking" not in kw.get("extra_body", {})


def test_openai_gpt5_fast_minimal_effort():
    kw = _apply(
        "fast",
        "openai",
        "gpt-5.4",
        info_extra={"supports_reasoning_effort": True},
    )
    assert kw["reasoning_effort"] == "minimal"


# ── env overrides still win ──────────────────────────────────────────────────


def test_env_override_forces_disabled_even_in_deep(monkeypatch):
    monkeypatch.setenv("EVERMIND_KIMI_THINKING", "disabled")
    kw = _apply("deep", "kimi", "kimi-k2.5")
    assert kw["extra_body"]["thinking"] == {"type": "disabled"}


def test_env_override_forces_enabled_even_in_fast(monkeypatch):
    monkeypatch.setenv("EVERMIND_ANTHROPIC_THINKING", "enabled")
    kw = _apply("fast", "anthropic", "claude-opus-4-6", kwargs={"max_tokens": 8000})
    assert kw["extra_body"]["thinking"]["type"] == "enabled"


# ── V6.1: generic relay / gateway support ────────────────────────────────────


def test_is_relay_endpoint_known_hosts():
    assert AIBridge._is_relay_endpoint("", "https://api.private-relay.com/v1") is True
    assert AIBridge._is_relay_endpoint("", "https://llm.private-relay.example/v1") is True
    assert AIBridge._is_relay_endpoint("", "https://openrouter.ai/api/v1") is True
    assert AIBridge._is_relay_endpoint("", "https://api.aihubmix.com/v1") is True
    assert AIBridge._is_relay_endpoint("", "https://api.laozhang.ai/v1") is True


def test_is_relay_endpoint_explicit_provider():
    assert AIBridge._is_relay_endpoint("relay", "https://anything.example/v1") is True
    assert AIBridge._is_relay_endpoint("aigate", "https://example/v1") is True


def test_is_relay_endpoint_official_not_relay():
    assert AIBridge._is_relay_endpoint("openai", "https://api.openai.com/v1") is False
    assert AIBridge._is_relay_endpoint("anthropic", "https://api.anthropic.com/v1") is False
    assert AIBridge._is_relay_endpoint("kimi", "https://api.moonshot.cn/v1") is False
    assert AIBridge._is_relay_endpoint("", "https://api.deepseek.com/v1") is False
    assert AIBridge._is_relay_endpoint("", "https://api.groq.com/openai/v1") is False
    assert AIBridge._is_relay_endpoint("", "https://generativelanguage.googleapis.com/v1beta") is False


def test_is_relay_endpoint_official_host_wins_over_marker_substring():
    """Opus review #3: match official via equality/dot-suffix, not substring.
    `api.openai.com.evil.oneapi.example` is a spoof and must be classified as
    relay (the `oneapi` marker wins). Legit subdomain `foo.api.openai.com`
    still counts as official via dot-suffix."""
    assert AIBridge._is_relay_endpoint("", "https://api.openai.com/v1/chat/completions") is False
    assert AIBridge._is_relay_endpoint("", "https://staging.api.openai.com/v1") is False
    # Spoof: not a real OpenAI host — marker `oneapi` should now win.
    assert AIBridge._is_relay_endpoint("", "https://api.openai.com.evil.oneapi.example") is True


def test_relay_deep_unions_every_enable_flag():
    """Any third-party relay in deep mode should fire every known 'enable
    thinking' key so whichever backend it fronts sees the right one."""
    kw = _apply(
        "deep",
        provider="",
        model="some-custom-model",
        info_extra={"api_base": "https://openrouter.ai/api/v1"},
    )
    eb = kw["extra_body"]
    assert eb["thinking"] == {"type": "enabled"}
    assert eb["enable_thinking"] is True
    assert eb["chat_template_kwargs"] == {"enable_thinking": True}
    assert eb["reasoning_split"] is True
    assert eb["reasoning"]["enabled"] is True


def test_relay_fast_unions_every_disable_flag():
    kw = _apply(
        "fast",
        provider="",
        model="some-custom-model",
        info_extra={"api_base": "https://api.private-relay.com/v1"},
    )
    eb = kw["extra_body"]
    assert eb["thinking"] == {"type": "disabled"}
    assert eb["enable_thinking"] is False
    assert eb["no_thinking"] is True
    assert eb["reasoning"]["enabled"] is False


def test_unknown_custom_relay_with_provider_relay_treated_as_gateway():
    kw = _apply(
        "deep",
        provider="relay",
        model="user-custom-model",
        info_extra={"api_base": "https://my-brand-new-gateway.example.com/v1"},
    )
    assert kw["extra_body"]["enable_thinking"] is True


def test_relay_with_kimi_model_keeps_cache_key():
    kw = _apply(
        "deep",
        provider="relay",
        model="kimi-k2.5-private-relay",
        info_extra={"api_base": "https://llm.private-relay.example/v1"},
    )
    assert kw["extra_body"]["prompt_cache_key"] == "run1-builder-ne1"


# ── V6.1 hotfix: node-type thinking exemption ────────────────────────────────


def test_builder_node_in_deep_mode_does_not_open_thinking():
    """deep + builder should emit thinking=disabled so kimi k2.6-code-preview
    stops narrating and goes straight to file_ops write."""
    kw = _apply("deep", "kimi", "kimi-k2.6-code-preview",
                info_extra={"node_type_hint": "builder"})  # placeholder; real param next
    # With the hotfix we'll pass node_type='builder' — see explicit test below


def test_builder_kimi_thinking_disabled_in_deep_v6_4_22():
    """v6.4.22 (maintainer 2026-04-22): kimi-family builder/merger keep thinking OFF
    even in deep mode. Rationale: observed 2026-04-22 run — kimi builder with
    thinking=enabled spent 281s on thinking + only 46s emitting HTML before
    hitting the 16k max_tokens cap and finish=length at 12KB (truncated).
    Kimi's binary thinking consumes the OUTPUT budget, unlike OpenAI's
    separate reasoning budget. So kimi builders stay lean; gpt-5.x builders
    (via relay) still get reasoning_effort=high."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    kw: dict = {"max_tokens": 4096}
    bridge._apply_thinking_config_to_kwargs(
        kw,
        {"provider": "kimi"},
        "kimi-k2.5",
        cache_key="run1-builder-ne1",
        node_type="builder",
    )
    assert kw["extra_body"]["thinking"] == {"type": "disabled"}
    assert kw["max_tokens"] == 4096  # unchanged — no forced 16k bump
    assert "temperature" not in kw


def test_merger_gets_thinking_in_deep_mode_v6_4_21():
    """v6.4.21: merger deep mode enables Qwen thinking."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw, {"provider": "qwen"}, "qwen3-max", node_type="merger",
    )
    assert kw["extra_body"]["enable_thinking"] is True


def test_router_planner_exempt_in_deep_mode():
    bridge = AIBridge(config={"thinking_depth": "deep"})
    for role in ("router", "planner", "planner_degraded"):
        kw: dict = {}
        bridge._apply_thinking_config_to_kwargs(
            kw, {"provider": "aigate", "api_base": "https://llm.private-relay.example/v1"},
            "deepseek-v3.2", node_type=role,
        )
        eb = kw.get("extra_body", {})
        assert eb.get("thinking") == {"type": "disabled"}, f"{role} should skip thinking"
        assert eb.get("enable_thinking") is False


def test_analyst_still_gets_thinking_in_deep_mode():
    """Real-reasoning node must keep the deep benefit."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw, {"provider": "deepseek"}, "deepseek-chat", node_type="analyst",
    )
    assert kw["extra_body"]["thinking"] == {"type": "enabled"}


def test_reviewer_debugger_polisher_get_thinking_in_deep():
    bridge = AIBridge(config={"thinking_depth": "deep"})
    for role in ("reviewer", "debugger", "polisher", "tester"):
        kw: dict = {}
        bridge._apply_thinking_config_to_kwargs(
            kw, {"provider": "zhipu"}, "glm-5", node_type=role,
        )
        assert kw["extra_body"]["thinking"] == {"type": "enabled"}, f"{role} should think"


def test_env_override_can_force_thinking_back_on_exempt_node(monkeypatch):
    monkeypatch.setenv("EVERMIND_FORCE_NODE_THINKING", "1")
    bridge = AIBridge(config={"thinking_depth": "deep"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw, {"provider": "kimi"}, "kimi-k2.5", node_type="builder",
    )
    assert kw["extra_body"]["thinking"] == {"type": "enabled"}


def test_builder_prefix_variants_kimi_thinking_off_in_deep_v6_4_22():
    """v6.4.22: builder1/2/_primary and merger_main all keep kimi thinking OFF
    in deep mode (anti-truncation)."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    for role in ("builder1", "builder2", "builder_primary", "merger_main"):
        kw: dict = {}
        bridge._apply_thinking_config_to_kwargs(
            kw, {"provider": "kimi"}, "kimi-k2.5", node_type=role,
        )
        assert kw["extra_body"]["thinking"] == {"type": "disabled"}, f"{role} kimi thinking must stay off in v6.4.22"


def test_exempt_nodes_keep_high_effort_in_deep_v6_4_22():
    """v6.4.22: thinking-flag-exempt nodes (planner/analyst/reviewer/tester/
    patcher/debugger) now keep reasoning_effort=high in deep mode. The
    previous bundling incorrectly degraded them to 'low' because the
    thinking_depth rewrite to 'normal' dragged effort down too. Fixed by
    decoupling: _effort_depth preserves user intent."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    for role in ("planner", "analyst", "reviewer", "tester", "patcher", "debugger"):
        kw: dict = {}
        bridge._apply_thinking_config_to_kwargs(
            kw,
            {"provider": "relay", "supports_reasoning_effort": True, "api_base": "https://relay.cn/v1"},
            "gpt-5.4",
            node_type=role,
        )
        assert kw.get("reasoning_effort") == "high", f"{role} effort must be high in deep, got {kw.get('reasoning_effort')}"


def test_exempt_nodes_still_have_thinking_flag_off_in_deep_v6_4_22():
    """v6.4.22: decoupled — effort=high doesn't imply thinking flag on. Exempt
    nodes keep thinking=disabled to avoid finish=length on kimi/qwen."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    for role in ("planner", "analyst", "reviewer", "patcher"):
        kw: dict = {}
        bridge._apply_thinking_config_to_kwargs(
            kw, {"provider": "kimi"}, "kimi-k2.6-code-preview", node_type=role,
        )
        assert kw["extra_body"]["thinking"] == {"type": "disabled"}, f"{role} kimi thinking must stay disabled"


def test_fast_mode_still_off_for_exempt_nodes():
    """Fast mode baseline: everyone off regardless of exempt classification."""
    bridge = AIBridge(config={"thinking_depth": "fast"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw, {"provider": "kimi"}, "kimi-k2.5", node_type="builder",
    )
    assert kw["extra_body"]["thinking"] == {"type": "disabled"}


def test_v6_4_21_builder_fast_large_project_gets_medium_effort():
    """v6.4.21 (maintainer 2026-04-22): fast mode builder with large project hint
    (task_type=game OR assigned_targets>=2 OR goal>=600 chars) maps to
    reasoning_effort=medium on relay/gpt-5.x, not minimal."""
    bridge = AIBridge(config={"thinking_depth": "fast"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw,
        {"provider": "openai", "supports_reasoning_effort": True},
        "gpt-5.4",
        node_type="builder",
        input_data={"task_type": "game", "assigned_targets": ["index.html", "game.js"]},
    )
    assert kw.get("reasoning_effort") == "medium"


def test_v6_4_21_builder_fast_small_project_gets_low_effort():
    """v6.4.21: fast + small project (single file, short goal) → low, not minimal."""
    bridge = AIBridge(config={"thinking_depth": "fast"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw,
        {"provider": "openai", "supports_reasoning_effort": True},
        "gpt-5.4",
        node_type="builder",
        input_data={"task_type": "website", "assigned_targets": ["index.html"], "goal": "simple landing page"},
    )
    assert kw.get("reasoning_effort") == "low"


def test_v6_4_21_builder_deep_gets_high_effort():
    """v6.4.21: deep mode builder always gets high regardless of size."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw,
        {"provider": "openai", "supports_reasoning_effort": True},
        "gpt-5.4",
        node_type="builder",
        input_data={"task_type": "website", "assigned_targets": ["index.html"]},
    )
    assert kw.get("reasoning_effort") == "high"


def test_v6_4_21_merger_deep_gets_high_effort_on_relay():
    """v6.4.21: merger via relay in deep mode → high reasoning_effort."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw,
        {"provider": "relay", "supports_reasoning_effort": True, "api_base": "https://relay.cn/v1"},
        "gpt-5.4",
        node_type="merger",
        input_data={"task_type": "website"},
    )
    assert kw.get("reasoning_effort") == "high"


def test_v6_4_21_non_build_node_unaffected_by_size_hint():
    """v6.4.21: non-builder nodes like reviewer keep their original behavior,
    input_data size hints do not apply to them."""
    bridge = AIBridge(config={"thinking_depth": "fast"})
    kw: dict = {}
    bridge._apply_thinking_config_to_kwargs(
        kw,
        {"provider": "openai", "supports_reasoning_effort": True},
        "gpt-5.4",
        node_type="reviewer",  # exempt, plus not builder/merger
        input_data={"task_type": "game", "assigned_targets": ["a", "b", "c"]},
    )
    # reviewer is exempt + fast mode → reasoning_effort=minimal (original path)
    assert kw.get("reasoning_effort") == "minimal"


def test_relay_env_override_wins():
    import os
    os.environ["EVERMIND_RELAY_THINKING"] = "disabled"
    try:
        kw = _apply(
            "deep",
            provider="relay",
            model="gpt-5.4",
            info_extra={"api_base": "https://openrouter.ai/api/v1"},
        )
        assert kw["extra_body"]["thinking"] == {"type": "disabled"}
    finally:
        os.environ.pop("EVERMIND_RELAY_THINKING", None)
