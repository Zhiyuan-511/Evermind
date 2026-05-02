"""Unit tests for backend/providers/ plugin layer.

Covers registry matching, request normalization, retry-policy decisions,
MiniMax <think>-tag stripping, DeepSeek multi-turn reasoning rules,
and the cross-vendor aggregation contract.
"""

from __future__ import annotations

import pytest

from providers import (
    ChatChunk,
    ChatRequest,
    known_providers,
    resolve_provider,
)
from providers.anthropic import AnthropicProvider
from providers.base import OpenAICompatProvider
from providers.deepseek import DeepSeekProvider
from providers.doubao import DoubaoProvider
from providers.gemini import GeminiProvider
from providers.kimi import KimiProvider
from providers.meta_llama import MetaLlamaProvider
from providers.minimax import MiniMaxProvider, _strip_think_tags
from providers.mistral import MistralProvider
from providers.openai import OpenAIProvider
from providers.qwen import QwenProvider
from providers.xai import XAIProvider
from providers.zhipu import ZhipuProvider


# ── registry ────────────────────────────────────────────────────────────

class TestRegistry:
    def test_all_twelve_registered(self):
        names = known_providers()
        assert set(names) >= {
            "deepseek", "kimi", "qwen", "zhipu", "doubao", "minimax",
            "openai", "anthropic", "gemini", "xai", "mistral", "llama",
        }

    @pytest.mark.parametrize("model,expected", [
        # Chinese
        ("kimi-k2.6-code-preview", "kimi"),
        ("kimi-coding", "kimi"),
        ("moonshot-v1-32k", "kimi"),
        ("deepseek-chat", "deepseek"),
        ("deepseek-reasoner", "deepseek"),
        ("DeepSeek-V3", "deepseek"),
        ("qwen-max-latest", "qwen"),
        ("qwen3-coder-plus", "qwen"),
        ("qwq-32b", "qwen"),
        ("glm-4.6", "zhipu"),
        ("GLM-5.1", "zhipu"),
        ("chatglm-turbo", "zhipu"),
        ("doubao-seed-1-6", "doubao"),
        ("ep-20260418-xxxxx", "doubao"),
        ("MiniMax-M2.7", "minimax"),
        ("abab6.5-chat", "minimax"),
        # Western
        ("gpt-5.4", "openai"),
        ("gpt-4o", "openai"),
        ("o1-preview", "openai"),
        ("o3-mini", "openai"),
        ("claude-opus-4-7", "anthropic"),
        ("claude-sonnet-4-6", "anthropic"),
        ("gemini-2.5-pro", "gemini"),
        ("gemini-2.0-flash", "gemini"),
        ("grok-4", "xai"),
        ("grok-4.20-reasoning", "xai"),
        ("mistral-large-latest", "mistral"),
        ("codestral-2404", "mistral"),
        ("pixtral-large", "mistral"),
        ("meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8", "llama"),
        ("llama-3.3-70b-versatile", "llama"),
    ])
    def test_matcher(self, model, expected):
        p = resolve_provider(model, api_key="test")
        assert p is not None, f"no match for {model}"
        assert p.name == expected

    def test_unknown_model_returns_none(self):
        assert resolve_provider("random-fake-model", api_key="test") is None


# ── DeepSeek multi-turn reasoning rules ────────────────────────────────

class TestDeepSeekMultiTurn:
    def setup_method(self):
        self.p = DeepSeekProvider(api_key="test")

    def test_reasoner_drops_temperature_and_top_p(self):
        req = ChatRequest(
            model="deepseek-reasoner",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            top_p=0.9,
        )
        body = self.p.normalize_request(req)
        assert "temperature" not in body
        assert "top_p" not in body

    def test_non_reasoner_keeps_temperature(self):
        req = ChatRequest(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
        )
        body = self.p.normalize_request(req)
        assert body.get("temperature") == 0.7

    def test_tool_call_branch_must_include_reasoning_content(self):
        history = [{"role": "user", "content": "q"}]
        last = {
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "f", "arguments": "{}"}}],
            "reasoning_content": "cot thoughts",
        }
        out = self.p.build_next_turn(history, last, tool_results=None)
        assistant = out[-1]
        assert assistant["role"] == "assistant"
        assert "tool_calls" in assistant
        assert assistant["reasoning_content"] == "cot thoughts"

    def test_non_tool_branch_must_strip_reasoning_content(self):
        history = [{"role": "user", "content": "q"}]
        last = {
            "content": "final answer",
            "reasoning_content": "should not persist",
        }
        out = self.p.build_next_turn(history, last)
        assistant = out[-1]
        assert "reasoning_content" not in assistant
        assert assistant["content"] == "final answer"

    def test_tool_results_appended_as_tool_role(self):
        history = []
        last = {"content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "f", "arguments": "{}"}}]}
        tool_results = [{"tool_call_id": "c1", "content": "{\"ok\":true}"}]
        out = self.p.build_next_turn(history, last, tool_results=tool_results)
        assert out[-1]["role"] == "tool"
        assert out[-1]["tool_call_id"] == "c1"

    @pytest.mark.parametrize("status,expect_retry", [
        (400, False), (401, False), (402, False), (422, False),
        (429, True),
        (500, True), (503, True),
    ])
    def test_retry_policy(self, status, expect_retry):
        err = RuntimeError(f"HTTP {status} error")
        hint = self.p.on_error_retry(err, attempt=1)
        assert hint.should_retry == expect_retry


# ── Kimi endpoint routing + cache key ──────────────────────────────────

class TestKimi:
    def setup_method(self):
        self.p = KimiProvider(api_key="test")

    def test_prompt_cache_key_from_session_and_node(self):
        req = ChatRequest(
            model="kimi-k2.6",
            messages=[{"role": "user", "content": "hi"}],
            session_id="sess-abc",
            node_name="builder-1",
        )
        body = self.p.normalize_request(req)
        assert body["prompt_cache_key"] == "sess-abc:builder-1"

    def test_default_temperature_applied(self):
        req = ChatRequest(model="kimi-coding", messages=[])
        body = self.p.normalize_request(req)
        assert body["temperature"] == 0.6

    def test_coding_model_routes_to_coding_endpoint(self):
        assert self.p.endpoint_for_model("kimi-coding") == "https://api.kimi.com/coding/v1"
        assert self.p.endpoint_for_model("kimi-k2.6") == "https://api.moonshot.ai/v1"

    def test_coding_model_adds_ua_header(self):
        headers = self.p.headers_for_model("kimi-coding")
        assert headers.get("User-Agent") == "claude-code"

    def test_non_coding_no_ua_header(self):
        headers = self.p.headers_for_model("kimi-k2.6")
        assert "User-Agent" not in headers


# ── Qwen thinking-disable default + force stream ───────────────────────

class TestQwen:
    def setup_method(self):
        self.p = QwenProvider(api_key="test")

    def test_default_enables_stream_and_disables_thinking(self):
        req = ChatRequest(
            model="qwen-max",
            messages=[{"role": "user", "content": "x"}],
            stream=False,
        )
        body = self.p.normalize_request(req)
        assert body["stream"] is True  # forced
        assert body["extra_body"]["enable_thinking"] is False

    def test_want_thinking_true(self):
        req = ChatRequest(model="qwen-max", messages=[], want_thinking=True)
        body = self.p.normalize_request(req)
        assert body["extra_body"]["enable_thinking"] is True

    def test_thinking_only_model_skips_flag(self):
        req = ChatRequest(model="qwq-32b", messages=[])
        body = self.p.normalize_request(req)
        # thinking-only models shouldn't have the flag
        assert "enable_thinking" not in body.get("extra_body", {})

    def test_zero_temperature_bumped_to_safe_default(self):
        req = ChatRequest(model="qwen-max", messages=[], temperature=0.0)
        body = self.p.normalize_request(req)
        assert body["temperature"] == 0.7

    def test_thinking_param_mismatch_fatal(self):
        err = RuntimeError("HTTP 400: parameter.enable_thinking must be set to false for non-streaming calls")
        hint = self.p.on_error_retry(err, attempt=1)
        assert hint.should_retry is False


# ── Zhipu GLM thinking field + request_id ──────────────────────────────

class TestZhipu:
    def setup_method(self):
        self.p = ZhipuProvider(api_key="test")

    def test_default_thinking_disabled_and_request_id_generated(self):
        req = ChatRequest(model="glm-4.6", messages=[])
        body = self.p.normalize_request(req)
        assert body["thinking"] == {"type": "disabled"}
        req_id = body["extra_body"]["request_id"]
        assert isinstance(req_id, str) and len(req_id) >= 16

    def test_want_thinking_toggles_enabled(self):
        req = ChatRequest(model="glm-4.6", messages=[], want_thinking=True)
        body = self.p.normalize_request(req)
        assert body["thinking"] == {"type": "enabled"}

    def test_retry_overload_triggers_backoff(self):
        err = RuntimeError("APIServerFlowExceedError: server overloaded")
        hint = self.p.on_error_retry(err, attempt=1)
        assert hint.should_retry is True
        assert hint.backoff_seconds >= 5.0


# ── Doubao endpoint-id routing + thinking whitelist ────────────────────

class TestDoubao:
    def setup_method(self):
        self.p = DoubaoProvider(api_key="test")

    def test_matches_ep_prefix(self):
        assert self.p.matches("ep-20260418155555-xyz")

    def test_invalid_thinking_fallback(self):
        req = ChatRequest(model="doubao-seed-1-6", messages=[],
                          extra={"thinking_type": "bogus"})
        body = self.p.normalize_request(req)
        # bogus → we don't leak it, fall back to disabled
        assert body["extra_body"]["thinking"]["type"] == "disabled"

    def test_valid_thinking_override_respected(self):
        req = ChatRequest(model="doubao-seed-1-6", messages=[],
                          extra={"thinking_type": "auto"})
        body = self.p.normalize_request(req)
        assert body["extra_body"]["thinking"]["type"] == "auto"

    def test_endpoint_not_found_is_fatal(self):
        err = RuntimeError("HTTP 404: endpoint ep-xxx not found")
        hint = self.p.on_error_retry(err, attempt=1)
        assert hint.should_retry is False


# ── MiniMax <think> stripping + reasoning_split default ────────────────

class TestMiniMaxThinkStripping:
    def test_reasoning_split_default(self):
        p = MiniMaxProvider(api_key="test")
        body = p.normalize_request(ChatRequest(model="MiniMax-M2.7", messages=[]))
        assert body["extra_body"]["reasoning_split"] is True

    def test_strip_balanced_think_block(self):
        content, reasoning = _strip_think_tags("before<think>cot</think>after")
        assert content == "beforeafter"
        assert reasoning == "cot"

    def test_strip_multiple_think_blocks(self):
        content, reasoning = _strip_think_tags("<think>a</think>x<think>b</think>y")
        assert content == "xy"
        assert reasoning == "ab"

    def test_strip_dangling_open(self):
        content, reasoning = _strip_think_tags("hi<think>still thinking")
        assert content == "hi"
        assert reasoning == "still thinking"

    def test_strip_no_tags_passthrough(self):
        content, reasoning = _strip_think_tags("plain text")
        assert content == "plain text"
        assert reasoning == ""

    def test_case_insensitive_tags(self):
        content, reasoning = _strip_think_tags("<THINK>a</THINK>b")
        assert content == "b"
        assert reasoning == "a"

    @pytest.mark.parametrize("status,code,expect_retry", [
        (None, "error code 1001 timeout", True),
        (None, "error code 1002 rate limit", True),
        (None, "error code 1004 auth fail", False),
        (None, "error code 1008 balance", False),
        (None, "error code 2056 quota", False),
    ])
    def test_minimax_error_codes(self, status, code, expect_retry):
        p = MiniMaxProvider(api_key="test")
        err = RuntimeError(code)
        hint = p.on_error_retry(err, attempt=1)
        assert hint.should_retry == expect_retry


# ── Cross-vendor aggregation contract ──────────────────────────────────

class TestChatChunkAggregation:
    def _new_provider(self):
        class Dummy(OpenAICompatProvider):
            name = "dummy"

            @classmethod
            def matches(cls, model_name):
                return False

            def on_error_retry(self, err, attempt):
                from providers.base import ProviderRetryHint
                return ProviderRetryHint(False, 0.0, "")

        return Dummy(api_key="test")

    def test_content_concatenation(self):
        p = self._new_provider()
        chunks = [
            ChatChunk(content_delta="hello "),
            ChatChunk(content_delta="world"),
        ]
        content, reasoning, tools = p.extract_content(chunks)
        assert content == "hello world"
        assert reasoning == ""
        assert tools == []

    def test_tool_calls_merge_by_index(self):
        p = self._new_provider()
        chunks = [
            ChatChunk(tool_call_delta={"index": 0, "id": "c1",
                                        "function": {"name": "f",
                                                     "arguments": "{\"a\":"}}),
            ChatChunk(tool_call_delta={"index": 0,
                                        "function": {"name": "",
                                                     "arguments": "1}"}}),
            ChatChunk(tool_call_delta={"index": 1, "id": "c2",
                                        "function": {"name": "g",
                                                     "arguments": "{}"}}),
        ]
        content, reasoning, tools = p.extract_content(chunks)
        assert len(tools) == 2
        assert tools[0]["id"] == "c1"
        assert tools[0]["function"]["arguments"] == "{\"a\":1}"
        assert tools[1]["id"] == "c2"

    def test_reasoning_and_content_split(self):
        p = self._new_provider()
        chunks = [
            ChatChunk(reasoning_delta="thinking..."),
            ChatChunk(content_delta="final"),
        ]
        content, reasoning, tools = p.extract_content(chunks)
        assert content == "final"
        assert reasoning == "thinking..."


# ── Tool schema validation (Kimi regex) ────────────────────────────────

class TestToolValidation:
    def test_accepts_clean_openai_tool(self):
        p = DeepSeekProvider(api_key="test")
        err = p.validate_tools([{
            "type": "function",
            "function": {"name": "get_weather", "parameters": {}},
        }])
        assert err is None

    def test_rejects_dotted_name(self):
        p = DeepSeekProvider(api_key="test")
        err = p.validate_tools([{
            "type": "function",
            "function": {"name": "agent.file.read", "parameters": {}},
        }])
        assert err is not None and "regex" in err.lower() or "a-zA-Z" in (err or "")

    def test_rejects_empty_name(self):
        p = DeepSeekProvider(api_key="test")
        err = p.validate_tools([{"type": "function", "function": {}}])
        assert err is not None


# ── OpenAI reasoning model mapping ─────────────────────────────────────

class TestOpenAI:
    def setup_method(self):
        self.p = OpenAIProvider(api_key="test")

    def test_chat_gpt_keeps_params(self):
        req = ChatRequest(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            max_tokens=100,
        )
        body = self.p.normalize_request(req)
        assert body["temperature"] == 0.7
        assert body["max_tokens"] == 100

    def test_reasoning_model_swaps_max_tokens(self):
        req = ChatRequest(model="o1-preview", messages=[], max_tokens=1024, temperature=0.7)
        body = self.p.normalize_request(req)
        assert "max_tokens" not in body
        assert body["max_completion_tokens"] == 1024
        assert "temperature" not in body

    def test_gpt5_is_reasoning(self):
        req = ChatRequest(model="gpt-5.4", messages=[], temperature=1.0, max_tokens=500)
        body = self.p.normalize_request(req)
        assert "max_tokens" not in body
        assert "temperature" not in body

    def test_want_thinking_sets_effort(self):
        req = ChatRequest(model="gpt-5.4", messages=[], want_thinking=True)
        body = self.p.normalize_request(req)
        assert body.get("reasoning_effort") == "medium"


# ── Anthropic native Messages API shaping ──────────────────────────────

class TestAnthropic:
    def setup_method(self):
        self.p = AnthropicProvider(api_key="test")

    def test_system_lifted_to_top_level(self):
        req = ChatRequest(
            model="claude-opus-4-7",
            messages=[
                {"role": "system", "content": "you are X"},
                {"role": "user", "content": "hi"},
            ],
        )
        body = self.p.normalize_request(req)
        assert body["system"] == "you are X"
        assert all(m.get("role") != "system" for m in body["messages"])

    def test_max_tokens_required_default(self):
        req = ChatRequest(model="claude-opus-4-7", messages=[{"role": "user", "content": "hi"}])
        body = self.p.normalize_request(req)
        assert body["max_tokens"] >= 1024

    def test_tools_translated(self):
        req = ChatRequest(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}],
        )
        body = self.p.normalize_request(req)
        assert body["tools"][0]["name"] == "f"
        assert "input_schema" in body["tools"][0]
        assert "function" not in body["tools"][0]

    def test_tool_choice_required_maps_to_any(self):
        req = ChatRequest(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
            tool_choice="required",
        )
        body = self.p.normalize_request(req)
        assert body["tool_choice"]["type"] == "any"

    def test_want_thinking_sets_budget(self):
        req = ChatRequest(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "x"}],
            want_thinking=True,
            max_tokens=8000,
        )
        body = self.p.normalize_request(req)
        assert body["thinking"]["type"] == "enabled"
        assert body["thinking"]["budget_tokens"] >= 1024
        assert body["thinking"]["budget_tokens"] < 8000

    def test_parse_text_delta(self):
        # Simulate an Anthropic SSE event
        evt = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "hello"}}
        chunk = self.p.parse_stream_chunk(evt)
        assert chunk is not None
        assert chunk.content_delta == "hello"

    def test_parse_thinking_delta(self):
        evt = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "thinking_delta", "thinking": "reasoning"}}
        chunk = self.p.parse_stream_chunk(evt)
        assert chunk is not None
        assert chunk.reasoning_delta == "reasoning"

    def test_parse_tool_use_start(self):
        evt = {"type": "content_block_start", "index": 1,
               "content_block": {"type": "tool_use", "id": "toolu_x", "name": "f", "input": {}}}
        chunk = self.p.parse_stream_chunk(evt)
        assert chunk is not None
        assert chunk.tool_call_delta["id"] == "toolu_x"
        assert chunk.tool_call_delta["function"]["name"] == "f"

    def test_stop_reason_mapping(self):
        evt = {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}
        chunk = self.p.parse_stream_chunk(evt)
        assert chunk is not None
        assert chunk.finish_reason == "tool_calls"


# ── Gemini shape + role mapping ────────────────────────────────────────

class TestGemini:
    def setup_method(self):
        self.p = GeminiProvider(api_key="test")

    def test_role_remap_assistant_to_model(self):
        req = ChatRequest(
            model="gemini-2.5-pro",
            messages=[
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "u2"},
            ],
        )
        body = self.p.normalize_request(req)
        roles = [m["role"] for m in body["contents"]]
        assert roles == ["user", "model", "user"]
        assert body["systemInstruction"]["parts"][0]["text"] == "s"

    def test_tools_translated_to_function_declarations(self):
        req = ChatRequest(
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}],
        )
        body = self.p.normalize_request(req)
        assert "functionDeclarations" in body["tools"][0]
        assert body["tools"][0]["functionDeclarations"][0]["name"] == "f"

    def test_tool_choice_required_maps_to_any_mode(self):
        req = ChatRequest(
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
            tool_choice="required",
        )
        body = self.p.normalize_request(req)
        assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"

    def test_parse_text_frame(self):
        raw = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": None}]}
        chunk = self.p.parse_stream_chunk(raw)
        assert chunk is not None
        assert chunk.content_delta == "hello"

    def test_parse_function_call_frame(self):
        raw = {"candidates": [{"content": {"parts": [
            {"functionCall": {"id": "fc1", "name": "f", "args": {"a": 1}}},
        ]}}]}
        chunk = self.p.parse_stream_chunk(raw)
        assert chunk is not None
        tcd = chunk.tool_call_delta
        assert tcd["id"] == "fc1"
        assert tcd["function"]["name"] == "f"
        assert '"a": 1' in tcd["function"]["arguments"]

    def test_finish_reason_mapping(self):
        raw = {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "MAX_TOKENS"}]}
        chunk = self.p.parse_stream_chunk(raw)
        assert chunk.finish_reason == "length"


# ── xAI Grok specifics ────────────────────────────────────────────────

class TestXAI:
    def setup_method(self):
        self.p = XAIProvider(api_key="test")

    def test_matches_grok(self):
        assert self.p.matches("grok-4")
        assert self.p.matches("grok-4.20-reasoning")

    def test_reasoning_effort_on_want_thinking(self):
        req = ChatRequest(model="grok-4-reasoning", messages=[], want_thinking=True)
        body = self.p.normalize_request(req)
        assert body.get("reasoning_effort") == "medium"

    def test_live_search_sunset_fatal(self):
        err = RuntimeError("410 Gone: search_parameters is deprecated")
        hint = self.p.on_error_retry(err, attempt=1)
        assert hint.should_retry is False
        assert "sunset" in hint.reason


# ── Mistral tool_choice translation ────────────────────────────────────

class TestMistral:
    def setup_method(self):
        self.p = MistralProvider(api_key="test")

    def test_matches_mistral_family(self):
        for m in ["mistral-large-latest", "codestral-2404", "pixtral-large", "ministral-3b"]:
            assert self.p.matches(m), m

    def test_tool_choice_required_maps_to_any(self):
        req = ChatRequest(
            model="mistral-large-latest",
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
            tool_choice="required",
        )
        body = self.p.normalize_request(req)
        assert body["tool_choice"] == "any"


# ── Meta Llama hosting differences ────────────────────────────────────

class TestMetaLlama:
    def setup_method(self):
        self.p = MetaLlamaProvider(api_key="test")

    def test_want_thinking_sets_reasoning_extension(self):
        req = ChatRequest(model="meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
                          messages=[], want_thinking=True)
        body = self.p.normalize_request(req)
        assert body["reasoning"]["enabled"] is True

    def test_422_marks_unsupported_feature(self):
        err = RuntimeError("HTTP 422: unsupported feature")
        hint = self.p.on_error_retry(err, attempt=1)
        assert hint.should_retry is False
        assert hint.reason == "unsupported-feature"
