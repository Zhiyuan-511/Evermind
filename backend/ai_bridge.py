"""
Evermind Backend — AI Bridge v3 (LiteLLM Unified Interface)
Supports 100+ LLM models through a single interface.
References: https://github.com/BerriAI/litellm
"""

import asyncio
import base64
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

from plugins.base import Plugin, PluginResult, PluginRegistry
from privacy import get_masker, PrivacyMasker
from proxy_relay import get_relay_manager

logger = logging.getLogger("evermind.ai_bridge")

# ─────────────────────────────────────────────
# Security — sanitize error messages to remove API keys
# ─────────────────────────────────────────────
_SENSITIVE_RE = re.compile(
    r"(?:sk|key|token|api[_-]?key|Bearer)[-_\s]?[a-zA-Z0-9._\-]{8,}",
    re.IGNORECASE,
)


def _sanitize_error(msg: str) -> str:
    """Strip potential API keys / secrets from error messages."""
    return _SENSITIVE_RE.sub("[REDACTED]", msg) if msg else msg

# ─────────────────────────────────────────────
# Model Registry — all supported models
# ─────────────────────────────────────────────
MODEL_REGISTRY = {
    # OpenAI
    "gpt-5.4": {"provider": "openai", "litellm_id": "gpt-5.4", "supports_tools": True, "supports_cua": True},
    "gpt-4.1": {"provider": "openai", "litellm_id": "gpt-4.1", "supports_tools": True, "supports_cua": False},
    "gpt-4o": {"provider": "openai", "litellm_id": "gpt-4o", "supports_tools": True, "supports_cua": False},
    "o3": {"provider": "openai", "litellm_id": "o3", "supports_tools": True, "supports_cua": False},
    # Anthropic
    "claude-4-sonnet": {"provider": "anthropic", "litellm_id": "claude-4-sonnet-20260514", "supports_tools": True, "supports_cua": False},
    "claude-4-opus": {"provider": "anthropic", "litellm_id": "claude-4-opus-20260514", "supports_tools": True, "supports_cua": False},
    "claude-3.5-sonnet": {"provider": "anthropic", "litellm_id": "claude-3-5-sonnet-20241022", "supports_tools": True, "supports_cua": False},
    # Google
    "gemini-2.5-pro": {"provider": "google", "litellm_id": "gemini/gemini-2.5-pro-preview-06-05", "supports_tools": True, "supports_cua": False},
    "gemini-2.0-flash": {"provider": "google", "litellm_id": "gemini/gemini-2.0-flash", "supports_tools": True, "supports_cua": False},
    # DeepSeek
    "deepseek-v3": {"provider": "deepseek", "litellm_id": "deepseek/deepseek-chat", "supports_tools": True, "supports_cua": False},
    "deepseek-r1": {"provider": "deepseek", "litellm_id": "deepseek/deepseek-reasoner", "supports_tools": False, "supports_cua": False},
    # Kimi / Moonshot
    "kimi": {"provider": "kimi", "litellm_id": "openai/moonshot-v1-128k", "supports_tools": True, "supports_cua": False,
             "api_base": "https://api.moonshot.cn/v1"},
    # Qwen / 通义千问
    "qwen-max": {"provider": "qwen", "litellm_id": "openai/qwen-max", "supports_tools": True, "supports_cua": False,
                 "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    # Local / Ollama
    "ollama-llama3": {"provider": "ollama", "litellm_id": "ollama/llama3", "supports_tools": False, "supports_cua": False},
    "ollama-qwen2.5": {"provider": "ollama", "litellm_id": "ollama/qwen2.5", "supports_tools": False, "supports_cua": False},
}

# ─────────────────────────────────────────────
# Agent Presets
# ─────────────────────────────────────────────
AGENT_PRESETS = {
    "router": {
        "instructions": (
            "You are a task router and planner. Analyze the user's request and output a JSON plan.\n"
            "Format: {\"subtasks\": [{\"agent\": \"builder|tester|reviewer|deployer\", \"task\": \"description\", \"depends_on\": []}]}\n"
            "Each subtask should have a clear agent assignment and description."
        ),
    },
    "planner": {
        "instructions": (
            "You are a senior project planner. Break complex goals into concrete subtasks.\n"
            "Consider dependencies between tasks. Output a structured plan with phases."
        ),
    },
    "builder": {
        "instructions": (
            "You are a senior software engineer. Write clean, production-ready code.\n"
            "You have access to: file operations, shell commands, git, and browser.\n"
            "Always explain your approach, write the code, then verify it compiles/runs.\n"
            "Output your code in the specified files. Report what you created."
        ),
    },
    "tester": {
        "instructions": (
            "You are a QA engineer. Test software thoroughly.\n"
            "Run the code/application and verify it works correctly.\n"
            "If tests PASS: output {\"status\": \"pass\", \"details\": \"...\"}\n"
            "If tests FAIL: output {\"status\": \"fail\", \"errors\": [...], \"suggestion\": \"...\"}\n"
            "Be specific about what failed and how to fix it."
        ),
    },
    "reviewer": {
        "instructions": (
            "You are a code reviewer. Analyze code for bugs, security issues, and best practices.\n"
            "If code is good: {\"status\": \"approved\", \"notes\": \"...\"}\n"
            "If issues found: {\"status\": \"needs_changes\", \"issues\": [...], \"fixes\": [...]}\n"
        ),
    },
    "deployer": {
        "instructions": "You are a DevOps engineer. Handle deployment, packaging, and infrastructure.",
    },
    "debugger": {
        "instructions": (
            "You are a debugging expert. Analyze error messages and failed tests.\n"
            "Identify root causes and provide specific fixes with code patches."
        ),
    },
    "analyst": {
        "instructions": "You are a data analyst. Analyze data, create reports, and provide insights.",
    },
    "scribe": {
        "instructions": "You are a technical writer. Create clear documentation, guides, and reports.",
    },
}


class AIBridge:
    """
    Unified AI execution engine with LiteLLM for 100+ model support.
    3 execution paths:
      1. CUA Responses Loop (GPT-5.4 computer use)
      2. LiteLLM function calling (any model with tools)
      3. LiteLLM direct chat (models without tool support)
    """

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self._openai_client = None
        self._openai_api_key = None
        self._setup_litellm()

    def _setup_litellm(self):
        """Configure LiteLLM with API keys from config/env."""
        try:
            import litellm
            litellm.set_verbose = False
            key_map = {
                "openai_api_key": "OPENAI_API_KEY",
                "anthropic_api_key": "ANTHROPIC_API_KEY",
                "gemini_api_key": "GEMINI_API_KEY",
                "deepseek_api_key": "DEEPSEEK_API_KEY",
                "kimi_api_key": "KIMI_API_KEY",
                "qwen_api_key": "QWEN_API_KEY",
            }
            for config_key, env_key in key_map.items():
                if config_key in self.config:
                    value = self.config.get(config_key, "")
                    if value:
                        os.environ[env_key] = value
                    else:
                        os.environ.pop(env_key, None)
            self._openai_client = None
            self._openai_api_key = None
            self._litellm = litellm
            logger.info("LiteLLM initialized — 100+ models available")
        except ImportError:
            self._litellm = None
            logger.warning("LiteLLM not installed, falling back to direct API calls")

    async def _get_openai(self):
        api_key = self.config.get("openai_api_key") or os.getenv("OPENAI_API_KEY")
        if api_key and (not self._openai_client or api_key != self._openai_api_key):
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=api_key)
            self._openai_api_key = api_key
        return self._openai_client

    def get_available_models(self) -> List[Dict]:
        """Return list of available models including relay models."""
        models = [{"id": k, **v} for k, v in MODEL_REGISTRY.items()]
        # Add relay models
        relay_mgr = get_relay_manager()
        for model_id, info in relay_mgr.get_all_models().items():
            models.append({"id": model_id, **info})
        return models

    def _resolve_model(self, model_name: str) -> Dict:
        """Resolve model info from registry or relay endpoints."""
        # Check static registry first
        if model_name in MODEL_REGISTRY:
            return MODEL_REGISTRY[model_name]
        # Check relay models
        relay_mgr = get_relay_manager()
        relay_models = relay_mgr.get_all_models()
        if model_name in relay_models:
            return relay_models[model_name]
        # Fallback — treat as raw LiteLLM model ID
        return {"litellm_id": model_name, "supports_tools": True, "supports_cua": False}

    def _normalize_usage(self, usage: Any) -> Dict[str, int]:
        if not usage:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        elif hasattr(usage, "dict"):
            usage = usage.dict()
        elif not isinstance(usage, dict):
            usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
            }

        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _merge_usage(self, base: Dict[str, int], delta: Any) -> Dict[str, int]:
        normalized = self._normalize_usage(delta)
        return {
            "prompt_tokens": base.get("prompt_tokens", 0) + normalized.get("prompt_tokens", 0),
            "completion_tokens": base.get("completion_tokens", 0) + normalized.get("completion_tokens", 0),
            "total_tokens": base.get("total_tokens", 0) + normalized.get("total_tokens", 0),
        }

    def _estimate_litellm_cost(self, response: Any, model: str) -> float:
        if not self._litellm:
            return 0.0
        try:
            return float(self._litellm.completion_cost(completion_response=response, model=model))
        except Exception:
            return 0.0

    def _estimate_response_cost(self, model: str, usage: Any) -> float:
        normalized = self._normalize_usage(usage)
        pricing = {
            "computer-use-preview": {
                "input_per_million": float(os.getenv("COMPUTER_USE_INPUT_COST_PER_MILLION", "2.5")),
                "output_per_million": float(os.getenv("COMPUTER_USE_OUTPUT_COST_PER_MILLION", "15.0")),
            },
            "gpt-4o": {
                "input_per_million": 2.5,
                "output_per_million": 10.0,
            },
        }
        rates = pricing.get(model)
        if not rates:
            return 0.0
        return (
            normalized.get("prompt_tokens", 0) * rates["input_per_million"] / 1_000_000
            + normalized.get("completion_tokens", 0) * rates["output_per_million"] / 1_000_000
        )

    # ─────────────────────────────────────────
    # Main dispatch
    # ─────────────────────────────────────────
    async def execute(self, node: Dict, plugins: List[Plugin], input_data: str,
                      model: str = "gpt-5.4", on_progress: Callable = None,
                      privacy_settings: Dict = None) -> Dict:
        node_type = node.get("type", "")
        node_model = node.get("model", model)

        # ── Privacy: mask PII before sending to AI ──
        masker = get_masker(privacy_settings) if privacy_settings else get_masker()
        masked_input, restore_map = masker.mask(input_data, node_type=node_type)
        if restore_map and on_progress:
            await on_progress({"stage": "privacy_masked", "pii_count": len(restore_map)})

        # Resolve model info (static registry + relay)
        model_info = self._resolve_model(node_model)

        # ── Execute with retry ──
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    wait = 2 ** attempt
                    logger.info(f"Retry {attempt}/{max_retries} for {node_model}, waiting {wait}s...")
                    if on_progress:
                        await on_progress({"stage": "retrying", "attempt": attempt, "wait": wait})
                    await asyncio.sleep(wait)

                # Path 1: CUA mode
                if model_info.get("supports_cua") and any(p.name == "computer_use" for p in plugins):
                    result = await self._execute_cua_loop(node, plugins, masked_input, on_progress)
                # Path 2: Relay endpoint
                elif model_info.get("provider") == "relay":
                    result = await self._execute_relay(node, masked_input, model_info, on_progress)
                # Path 3: LiteLLM with tools
                elif self._litellm and model_info.get("supports_tools") and plugins:
                    result = await self._execute_litellm_tools(node, plugins, masked_input, model_info, on_progress)
                # Path 4: LiteLLM direct chat
                elif self._litellm:
                    result = await self._execute_litellm_chat(node, masked_input, model_info, on_progress)
                # Fallback: direct OpenAI
                else:
                    result = await self._execute_openai_direct(node, plugins, masked_input, on_progress)

                if result.get("success"):
                    # ── Track usage ──
                    try:
                        from settings import get_usage_tracker
                        tracker = get_usage_tracker()
                        usage = self._normalize_usage(result.get("usage", {}))
                        tracker.record(
                            model=result.get("model", node_model),
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            cost=float(result.get("cost", 0) or 0),
                            provider=model_info.get("provider", "unknown"),
                            mode=result.get("mode", "unknown"),
                        )
                    except Exception:
                        pass
                    break  # Success, exit retry loop
                else:
                    last_error = result.get("error", "Unknown error")
                    # Don't retry on non-retryable errors
                    if "api key" in last_error.lower() or "auth" in last_error.lower():
                        break

            except Exception as e:
                last_error = str(e)
                logger.warning(f"Execute attempt {attempt+1} failed: {e}")
                result = {"success": False, "output": "", "error": _sanitize_error(last_error)}

        # ── Privacy: unmask PII in AI response ──
        if restore_map and result.get("output"):
            result["output"] = masker.unmask(result["output"], restore_map)
            result["privacy_masked"] = len(restore_map)

        return result

    # ─────────────────────────────────────────
    # Path: Relay Endpoint
    # ─────────────────────────────────────────
    async def _execute_relay(self, node, input_data, model_info, on_progress) -> Dict:
        """Execute through a proxy/relay endpoint."""
        relay_mgr = get_relay_manager()
        relay_id = model_info.get("relay_id")
        model_name = model_info["litellm_id"].replace("openai/", "")

        node_type = node.get("type", "builder")
        preset = AGENT_PRESETS.get(node_type, {})
        system_prompt = node.get("prompt") or preset.get("instructions", "You are a helpful assistant.")

        if on_progress:
            await on_progress({"stage": "calling_relay", "relay": model_info.get("relay_name", "?"), "model": model_name})

        result = await relay_mgr.call(
            endpoint_id=relay_id,
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data},
            ],
        )

        if result.get("success"):
            return {
                "success": True,
                "output": result.get("content", ""),
                "model": result.get("model", model_name),
                "tool_results": [],
                "mode": "relay",
                "relay": result.get("relay", ""),
                "usage": self._normalize_usage(result.get("usage", {})),
                "cost": float(result.get("cost", 0) or 0),
            }
        else:
            return {"success": False, "output": "", "error": _sanitize_error(result.get("error", "Relay call failed"))}

    # ─────────────────────────────────────────
    # Path 1: CUA Responses Loop
    # ─────────────────────────────────────────
    async def _execute_cua_loop(self, node, plugins, input_data, on_progress) -> Dict:
        client = await self._get_openai()
        if not client:
            return {"success": False, "output": "", "error": "OpenAI API key not configured"}

        if on_progress:
            await on_progress({"stage": "cua_start", "instruction": input_data[:100]})

        node_type = node.get("type", "builder")
        preset = AGENT_PRESETS.get(node_type, AGENT_PRESETS.get("builder", {}))
        system_prompt = node.get("prompt") or preset.get("instructions", "")

        tools = [{"type": "computer_use_preview", "display_width": 1920, "display_height": 1080, "environment": "mac"}]
        for p in plugins:
            if p.name != "computer_use":
                tools.append(p.get_tool_definition())

        input_messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": input_data}]
        all_artifacts, output_text, tool_results = [], "", []
        iteration, max_iterations = 0, 15
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        try:
            response = await client.responses.create(
                model="computer-use-preview", tools=tools, input=input_messages, truncation="auto"
            )
            usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
            while iteration < max_iterations:
                iteration += 1
                if on_progress:
                    await on_progress({"stage": "cua_iteration", "iteration": iteration, "max": max_iterations})
                has_action, new_input = False, []
                for item in response.output:
                    item_type = getattr(item, "type", None)
                    if item_type == "message":
                        for block in getattr(item, "content", []):
                            if hasattr(block, "text"):
                                output_text += block.text
                    elif item_type == "computer_call":
                        has_action = True
                        action = item.action
                        if on_progress:
                            await on_progress({"stage": "cua_action", "action": getattr(action, "type", "unknown"), "iteration": iteration})
                        await self._execute_cua_action(action, plugins)
                        ss_plugin = PluginRegistry.get("screenshot")
                        ss_b64 = ""
                        if ss_plugin:
                            ss_result = await ss_plugin.execute({}, context=self.config)
                            if ss_result.success and ss_result.artifacts:
                                ss_b64 = ss_result.artifacts[0].get("base64", "")
                                all_artifacts.append(ss_result.artifacts[0])
                        new_input.append({"type": "computer_call_output", "call_id": item.call_id,
                                          "output": {"type": "computer_screenshot", "image_url": f"data:image/png;base64,{ss_b64}"}})
                    elif item_type == "function_call":
                        has_action = True
                        result = await self._run_plugin(item.name, item.arguments, plugins)
                        tool_results.append(result)
                        new_input.append({"type": "function_call_output", "call_id": item.call_id, "output": json.dumps(result)})
                if not has_action:
                    break
                response = await client.responses.create(model="computer-use-preview", tools=tools, input=new_input, truncation="auto")
                usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
            return {"success": True, "output": output_text, "tool_results": tool_results, "artifacts": all_artifacts,
                    "model": "computer-use-preview", "iterations": iteration, "mode": "cua_loop", "usage": usage_totals,
                    "cost": self._estimate_response_cost("computer-use-preview", usage_totals)}
        except Exception as e:
            logger.error(f"CUA loop error: {_sanitize_error(str(e))}")
            return {"success": False, "output": output_text, "error": _sanitize_error(str(e))}

    async def _execute_cua_action(self, action, plugins) -> Dict:
        action_type = getattr(action, "type", "unknown")
        ui = PluginRegistry.get("ui_control")
        try:
            if action_type == "click" and ui:
                return (await ui.execute({"action": "click", "x": getattr(action, "x", 0), "y": getattr(action, "y", 0)}, context=self.config)).to_dict()
            elif action_type == "type" and ui:
                return (await ui.execute({"action": "type", "text": getattr(action, "text", "")}, context=self.config)).to_dict()
            elif action_type == "scroll" and ui:
                return (await ui.execute({"action": "scroll", "amount": getattr(action, "amount", -3)}, context=self.config)).to_dict()
            elif action_type == "key" and ui:
                return (await ui.execute({"action": "hotkey", "keys": getattr(action, "keys", [])}, context=self.config)).to_dict()
            elif action_type == "screenshot":
                ss = PluginRegistry.get("screenshot")
                if ss: return (await ss.execute({}, context=self.config)).to_dict()
            elif action_type == "wait":
                await asyncio.sleep(getattr(action, "duration", 1))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─────────────────────────────────────────
    # Path 2: LiteLLM with Tool Calling
    # ─────────────────────────────────────────
    async def _execute_litellm_tools(self, node, plugins, input_data, model_info, on_progress) -> Dict:
        node_type = node.get("type", "builder")
        preset = AGENT_PRESETS.get(node_type, {})
        system_prompt = node.get("prompt") or preset.get("instructions", "You are a helpful assistant.")
        litellm_model = model_info["litellm_id"]

        # Build OpenAI-format tools from plugins
        tools = []
        for p in plugins:
            if p.name != "computer_use":
                defn = p.get_tool_definition()
                tools.append({"type": "function", "function": defn} if "function" not in defn else defn)

        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": input_data}]

        if on_progress:
            await on_progress({"stage": "calling_ai", "model": litellm_model, "tools_count": len(tools)})

        try:
            kwargs = {"model": litellm_model, "messages": messages}
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            if model_info.get("api_base"):
                kwargs["api_base"] = model_info["api_base"]
            api_key_env = {
                "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"
            }.get(model_info.get("provider"))
            if api_key_env:
                kwargs["api_key"] = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

            response = await asyncio.to_thread(self._litellm.completion, **kwargs)

            output_text = ""
            tool_results = []
            iteration = 0
            max_iterations = 10
            usage_totals = self._normalize_usage(getattr(response, "usage", None))
            total_cost = self._estimate_litellm_cost(response, litellm_model)

            while iteration < max_iterations:
                iteration += 1
                msg = response.choices[0].message
                if msg.content:
                    output_text += msg.content

                # Check for tool calls
                if not msg.tool_calls:
                    break

                messages.append(msg)
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args = tc.function.arguments
                    if on_progress:
                        await on_progress({"stage": "executing_plugin", "plugin": fn_name})
                    result = await self._run_plugin(fn_name, fn_args, plugins)
                    tool_results.append(result)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

                if on_progress:
                    await on_progress({"stage": "continuing", "iteration": iteration})
                kwargs["messages"] = messages
                response = await asyncio.to_thread(self._litellm.completion, **kwargs)
                usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                total_cost += self._estimate_litellm_cost(response, litellm_model)

            return {"success": True, "output": output_text, "tool_results": tool_results,
                    "model": litellm_model, "iterations": iteration, "mode": "litellm_tools", "usage": usage_totals, "cost": total_cost}
        except Exception as e:
            logger.error(f"LiteLLM tools error: {_sanitize_error(str(e))}")
            return {"success": False, "output": "", "error": _sanitize_error(str(e))}

    # ─────────────────────────────────────────
    # Path 3: LiteLLM Direct Chat
    # ─────────────────────────────────────────
    async def _execute_litellm_chat(self, node, input_data, model_info, on_progress) -> Dict:
        node_type = node.get("type", "builder")
        preset = AGENT_PRESETS.get(node_type, {})
        system_prompt = node.get("prompt") or preset.get("instructions", "You are a helpful assistant.")
        litellm_model = model_info["litellm_id"]

        if on_progress:
            await on_progress({"stage": "calling_ai", "model": litellm_model, "tools_count": 0})

        try:
            kwargs = {"model": litellm_model, "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data}
            ]}
            if model_info.get("api_base"):
                kwargs["api_base"] = model_info["api_base"]
            api_key_env = {
                "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"
            }.get(model_info.get("provider"))
            if api_key_env:
                kwargs["api_key"] = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

            response = await asyncio.to_thread(self._litellm.completion, **kwargs)
            return {"success": True, "output": response.choices[0].message.content or "",
                    "model": litellm_model, "tool_results": [], "mode": "litellm_chat",
                    "usage": self._normalize_usage(getattr(response, "usage", None)),
                    "cost": self._estimate_litellm_cost(response, litellm_model)}
        except Exception as e:
            logger.error(f"LiteLLM chat error: {_sanitize_error(str(e))}")
            return {"success": False, "output": "", "error": _sanitize_error(str(e))}

    # ─────────────────────────────────────────
    # Fallback: Direct OpenAI
    # ─────────────────────────────────────────
    async def _execute_openai_direct(self, node, plugins, input_data, on_progress) -> Dict:
        client = await self._get_openai()
        if not client:
            return {"success": False, "output": "", "error": "No AI backend available"}
        preset = AGENT_PRESETS.get(node.get("type", "builder"), {})
        system_prompt = node.get("prompt") or preset.get("instructions", "You are a helpful assistant.")
        try:
            response = await client.chat.completions.create(
                model="gpt-4o", messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": input_data}
                ]
            )
            usage = self._normalize_usage(getattr(response, "usage", None))
            cost = self._estimate_litellm_cost(response, "gpt-4o") if self._litellm else self._estimate_response_cost("gpt-4o", usage)
            return {"success": True, "output": response.choices[0].message.content, "model": "gpt-4o", "tool_results": [], "mode": "openai_direct",
                    "usage": usage, "cost": cost}
        except Exception as e:
            return {"success": False, "output": "", "error": _sanitize_error(str(e))}

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────
    async def _run_plugin(self, name: str, args, plugins: List[Plugin]) -> Dict:
        plugin = next((p for p in plugins if p.name == name), None) or PluginRegistry.get(name)
        if not plugin:
            return {"error": f"Plugin {name} not found"}
        parsed = json.loads(args) if isinstance(args, str) else args
        result = await plugin.execute(parsed, context=self.config)
        return result.to_dict()


# ─────────────────────────────────────────────
# Handoff Manager
# ─────────────────────────────────────────────
class HandoffManager:
    """Agent-to-agent task delegation (OpenAI Agents SDK pattern)."""

    def __init__(self, ai_bridge: AIBridge):
        self.ai_bridge = ai_bridge

    async def handoff(self, from_node: Dict, to_node_type: str, task: str,
                      all_nodes: List[Dict], on_progress: Callable = None) -> Dict:
        target = next((n for n in all_nodes if n["type"] == to_node_type), None)
        if not target:
            return {"success": False, "error": f"No {to_node_type} node found"}
        if on_progress:
            await on_progress({"stage": "handoff", "from": from_node.get("name", ""), "to": target.get("name", ""), "task": task[:100]})
        from plugins.base import NODE_DEFAULT_PLUGINS
        enabled = target.get("plugins", NODE_DEFAULT_PLUGINS.get(to_node_type, []))
        plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]
        return await self.ai_bridge.execute(node=target, plugins=plugins,
                                            input_data=f"[Handoff from {from_node.get('name', '?')}] {task}", on_progress=on_progress)
