"""V6.1.2: reasoning_content provider-aware strip tests.

Prevents production 400s from DeepSeek (which rejects reasoning_content on
replay) and Anthropic (which needs only the LAST turn's thinking block).
"""

import pytest

from ai_bridge import AIBridge


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EVERMIND_REASONING_STRICT", "1")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    yield


def _msg(role, content="", **extra):
    m = {"role": role, "content": content}
    m.update(extra)
    return m


def _prepare(bridge: AIBridge, model: str, msgs):
    # route through the real pipeline so we hit layer 0.5
    return bridge._prepare_messages_for_request(list(msgs), model)


def test_deepseek_reasoning_content_fully_dropped():
    bridge = AIBridge(config={})
    msgs = [
        _msg("system", "You are helpful."),
        _msg("user", "hi"),
        _msg("assistant", "reply 1", reasoning_content="thought 1"),
        _msg("user", "again"),
        _msg("assistant", "reply 2", reasoning_content="thought 2"),
    ]
    out = _prepare(bridge, "deepseek-chat", msgs)
    for m in out:
        assert "reasoning_content" not in m, f"leaked on {m!r}"


def test_openai_gpt5_reasoning_fully_dropped():
    bridge = AIBridge(config={})
    msgs = [
        _msg("user", "hi"),
        _msg("assistant", "reply", reasoning_content="thought"),
    ]
    out = _prepare(bridge, "gpt-5.4", msgs)
    for m in out:
        assert "reasoning_content" not in m


def test_anthropic_keeps_only_last_assistant_reasoning():
    bridge = AIBridge(config={})
    msgs = [
        _msg("user", "q1"),
        _msg("assistant", "a1", reasoning_content="think 1"),
        _msg("user", "q2"),
        _msg("assistant", "a2", reasoning_content="think 2"),
        _msg("user", "q3"),
        _msg("assistant", "a3", reasoning_content="think 3"),
    ]
    out = _prepare(bridge, "claude-opus-4-7", msgs)
    kept = [m for m in out if m.get("role") == "assistant" and "reasoning_content" in m]
    assert len(kept) == 1
    # keep only the last (think 3); contents may be truncated by L1 so use prefix
    assert "think 3" in (kept[0]["reasoning_content"] or "")


def test_kimi_fast_mode_drops_reasoning():
    bridge = AIBridge(config={"thinking_depth": "fast"})
    msgs = [
        _msg("user", "hi"),
        _msg("assistant", "r", reasoning_content="t"),
    ]
    out = _prepare(bridge, "kimi-coding", msgs)
    for m in out:
        assert "reasoning_content" not in m


def test_kimi_deep_mode_keeps_reasoning():
    bridge = AIBridge(config={"thinking_depth": "deep"})
    msgs = [
        _msg("user", "hi"),
        _msg("assistant", "r", reasoning_content="internal chain of thought"),
    ]
    out = _prepare(bridge, "kimi-coding", msgs)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert any("internal" in (a.get("reasoning_content") or "") for a in assistants)


def test_qwen_thinking_keeps_reasoning():
    """Qwen has native thinking mode with reasoning_content replay support."""
    bridge = AIBridge(config={"thinking_depth": "deep"})
    msgs = [
        _msg("user", "hi"),
        _msg("assistant", "reply", reasoning_content="qwen thought"),
    ]
    out = _prepare(bridge, "qwen3-max", msgs)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert any("qwen thought" in (a.get("reasoning_content") or "") for a in assistants)


def test_strict_mode_can_be_disabled():
    import os
    os.environ["EVERMIND_REASONING_STRICT"] = "0"
    try:
        bridge = AIBridge(config={})
        msgs = [_msg("assistant", "r", reasoning_content="t")]
        out = _prepare(bridge, "deepseek-chat", msgs)
        # legacy behaviour: reasoning retained (truncated but present)
        assert any("reasoning_content" in m for m in out)
    finally:
        os.environ.pop("EVERMIND_REASONING_STRICT", None)
