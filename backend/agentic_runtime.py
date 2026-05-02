"""
Evermind v3.0 — Agentic Node Runtime Engine

Implements the Claude Code-style Think-Act-Observe loop with streaming tool
execution, context management, and sub-agent delegation.

References:
  - Claude Code (openclaude) agent-loop architecture
  - OpenClaw MCP server patterns
  - Cursor's parallel execution model

Each Evermind node can now autonomously:
  1. Analyze a task and formulate a strategy (Think)
  2. Select and execute tools with streaming (Act)
  3. Evaluate results and decide next steps (Observe)
  4. Manage its own context window efficiently
  5. Delegate sub-tasks to child agents
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger("evermind.agentic_runtime")


# ─── State Machine ───────────────────────────────────────

class LoopState(str, Enum):
    """Non-recursive state machine states for the agentic loop."""
    INIT = "init"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    COMPRESSING = "compressing"
    DELEGATING = "delegating"
    COMPLETED = "completed"
    FAILED = "failed"


# ─── Data Structures ─────────────────────────────────────

@dataclass
class ToolCall:
    """A single tool invocation extracted from LLM output."""
    id: str
    name: str
    arguments: Dict[str, Any]
    result: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    completed_at: float = 0.0
    error: Optional[str] = None

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at) * 1000
        return 0.0


@dataclass
class ThinkingTrace:
    """A record of the agent's reasoning at a point in time."""
    timestamp: float
    state: LoopState
    summary: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    token_count: int = 0
    context_percent: float = 0.0


@dataclass
class AgenticEvent:
    """An event emitted from the agentic loop for real-time UI updates."""
    timestamp: float
    event_type: str  # "thinking", "tool_start", "tool_end", "text_chunk", "context_compress", "delegate", "complete", "error"
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.timestamp,
            "event": self.event_type,
            **self.data,
        }


@dataclass
class ContextWindow:
    """Manages the message history and context compression."""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 128_000
    current_tokens: int = 0
    compression_threshold: float = 0.80  # Trigger compression at 80% capacity

    # Compression statistics
    snip_count: int = 0
    micro_compact_count: int = 0
    full_collapse_count: int = 0

    @property
    def usage_percent(self) -> float:
        if self.max_tokens <= 0:
            return 0.0
        return min(100.0, (self.current_tokens / self.max_tokens) * 100)

    @property
    def needs_compression(self) -> bool:
        return self.current_tokens > (self.max_tokens * self.compression_threshold)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate token count with CJK/ASCII-aware heuristic.

        v3.1: Replaces naive len//4 which severely underestimates CJK text
        (1.5-2 tokens/char) and overestimates pure ASCII code (~0.75 tokens/char).
        """
        cjk_chars = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]', text))
        ascii_chars = len(text) - cjk_chars
        return max(1, int(cjk_chars * 1.5 + ascii_chars * 0.75))

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        msg: Dict[str, Any] = {"role": role, "content": content}
        msg.update(kwargs)
        self.messages.append(msg)
        self.current_tokens += self._estimate_tokens(content)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })
        self.current_tokens += self._estimate_tokens(content)

    def snip_compact(self, max_tool_result_chars: int = 4000) -> int:
        """Level 1: Trim individual tool results that are too long."""
        trimmed = 0
        for msg in self.messages:
            if msg.get("role") == "tool":
                content = str(msg.get("content") or "")
                if len(content) > max_tool_result_chars:
                    original_tokens = self._estimate_tokens(content)
                    # Keep first 1000 and last 500 chars, summarize middle
                    msg["content"] = (
                        content[:1000]
                        + f"\n\n... [trimmed {len(content) - 1500} chars] ...\n\n"
                        + content[-500:]
                    )
                    new_tokens = self._estimate_tokens(msg["content"])
                    saved = original_tokens - new_tokens
                    self.current_tokens -= saved
                    trimmed += saved
        if trimmed > 0:
            self.snip_count += 1
        return trimmed

    def micro_compact(self, keep_recent: int = 3) -> int:
        """Level 2: Fold older conversation turns into summaries."""
        if len(self.messages) <= keep_recent * 2 + 1:  # system + recent pairs
            return 0

        # Keep system message + last N turns
        system_msgs = [m for m in self.messages if m.get("role") == "system"]
        non_system = [m for m in self.messages if m.get("role") != "system"]

        if len(non_system) <= keep_recent * 2:
            return 0

        old_msgs = non_system[:-keep_recent * 2]
        recent_msgs = non_system[-keep_recent * 2:]

        # Summarize old messages
        old_text_parts = []
        old_tokens = 0
        for msg in old_msgs:
            content = str(msg.get("content") or "")[:200]
            role = msg.get("role", "?")
            old_text_parts.append(f"[{role}] {content}")
            old_tokens += self._estimate_tokens(str(msg.get("content") or ""))

        summary = "[Previous context summary]\n" + "\n".join(old_text_parts[:10])
        if len(old_text_parts) > 10:
            summary += f"\n... and {len(old_text_parts) - 10} more exchanges"

        summary_tokens = self._estimate_tokens(summary)
        saved = old_tokens - summary_tokens

        self.messages = system_msgs + [{"role": "user", "content": summary}] + recent_msgs
        self.current_tokens -= saved
        self.micro_compact_count += 1
        return saved

    def full_collapse(self) -> int:
        """Level 3: Collapse the ENTIRE message history into a single summary.

        This is the nuclear option — used when L1+L2 compression is not enough
        and the context window is still dangerously full (>95%).
        """
        if len(self.messages) <= 2:
            return 0

        system_msgs = [m for m in self.messages if m.get("role") == "system"]
        non_system = [m for m in self.messages if m.get("role") != "system"]

        if not non_system:
            return 0

        old_tokens = self.current_tokens

        # Build a dense summary of ALL non-system content
        summary_parts = []
        files_mentioned = set()
        tools_used = set()
        key_outputs = []

        for msg in non_system:
            role = msg.get("role", "?")
            content = str(msg.get("content") or "")

            # Extract file references
            for token in content.split():
                if "/" in token and ("." in token.split("/")[-1]):
                    clean = token.strip("'\"`,;:()[]{}")
                    if len(clean) < 200:
                        files_mentioned.add(clean)

            # Track tool calls
            if msg.get("role") == "tool":
                tool_id = msg.get("tool_call_id", "")
                tools_used.add(tool_id[:20] if tool_id else "unknown")

            # Keep the last assistant output as key output
            if role == "assistant" and len(content) > 50:
                key_outputs.append(content[:500])

        summary_lines = [
            "[FULL CONTEXT COLLAPSE — Previous conversation compressed]",
            f"Messages collapsed: {len(non_system)}",
        ]
        if files_mentioned:
            summary_lines.append(f"Files referenced: {', '.join(sorted(files_mentioned)[:20])}")
        if tools_used:
            summary_lines.append(f"Tool calls made: {len(tools_used)}")
        if key_outputs:
            summary_lines.append(f"\nLast key output:\n{key_outputs[-1][:1000]}")

        collapsed_summary = "\n".join(summary_lines)
        collapsed_tokens = self._estimate_tokens(collapsed_summary)

        self.messages = system_msgs + [{"role": "user", "content": collapsed_summary}]
        self.current_tokens = sum(self._estimate_tokens(str(m.get("content", ""))) for m in self.messages)
        self.full_collapse_count += 1

        saved = old_tokens - self.current_tokens
        return max(0, saved)

    def get_messages(self) -> List[Dict[str, Any]]:
        return list(self.messages)


@dataclass
class LoopConfig:
    """Configuration for the agentic loop."""
    max_iterations: int = 25
    max_tool_calls: int = 50
    timeout_seconds: float = 600.0
    enable_thinking_trace: bool = True
    enable_sub_agents: bool = True
    enable_context_compression: bool = True
    node_type: str = "builder"
    node_key: str = ""
    model_name: str = ""
    # Which tools this node is allowed to use
    allowed_tools: List[str] = field(default_factory=list)
    # Role-specific configuration
    role_config: Dict[str, Any] = field(default_factory=dict)


# ─── Tool Registry ───────────────────────────────────────

class ToolRegistry:
    """Registry of tools available to agentic nodes."""

    def __init__(self) -> None:
        self._tools: Dict[str, "AgenticTool"] = {}

    def register(self, tool: "AgenticTool") -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional["AgenticTool"]:
        return self._tools.get(name)

    def list_tools(self, allowed: Optional[List[str]] = None) -> List["AgenticTool"]:
        if allowed is not None:
            return [t for name, t in self._tools.items() if name in allowed]
        return list(self._tools.values())

    def get_tool_definitions(self, allowed: Optional[List[str]] = None) -> List[Dict]:
        """Return OpenAI-format tool definitions."""
        tools = self.list_tools(allowed)
        return [
            {"type": "function", "function": t.get_definition()}
            for t in tools
        ]


class AgenticTool:
    """Base class for tools used in the agentic loop."""

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    def get_definition(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Any:
        raise NotImplementedError


# Each node role gets a specific set of tools.
# IMPORTANT: Only list tools that are ACTUALLY registered in agentic_tools.py TOOL_REGISTRY.
# v3.0: 11 tools implemented — file_read, file_write, file_edit, file_list, grep_search,
#   glob, web_fetch, web_search, bash, context_compress, multi_file_read
NODE_ROLE_TOOLS: Dict[str, List[str]] = {
    "planner": [
        "web_fetch", "web_search", "file_read", "file_list", "glob", "grep_search",
    ],
    "analyst": [
        "web_fetch", "web_search", "file_read", "file_list", "glob", "grep_search",
        "multi_file_read",
    ],
    "builder": [
        "file_read", "file_write", "file_edit", "file_list", "glob", "grep_search",
        "bash", "web_fetch", "web_search", "context_compress", "multi_file_read",
    ],
    "merger": [
        "file_read", "file_write", "file_edit", "file_list", "glob", "grep_search",
        "bash", "web_fetch", "web_search", "multi_file_read", "context_compress",
    ],
    "polisher": [
        "file_read", "file_write", "file_edit", "file_list", "glob", "grep_search",
        "bash", "web_fetch", "web_search", "multi_file_read", "context_compress",
    ],
    "reviewer": [
        "file_read", "file_list", "glob", "grep_search", "bash",
        "web_fetch", "web_search", "multi_file_read",
    ],
    "tester": [
        "file_read", "file_list", "glob", "grep_search", "bash", "multi_file_read",
    ],
    "debugger": [
        "file_read", "file_write", "file_edit", "file_list", "glob", "grep_search",
        "bash", "web_fetch", "web_search", "context_compress", "multi_file_read",
    ],
    "deployer": [
        "file_read", "file_list", "glob", "grep_search", "bash",
    ],
    "imagegen": [
        "web_fetch", "web_search", "file_write", "file_list",
    ],
    "spritesheet": [
        "file_read", "file_write", "file_list", "grep_search",
    ],
    "assetimport": [
        "file_read", "file_write", "file_list", "grep_search",
    ],
    "uidesign": [
        "web_fetch", "file_read", "file_list", "glob",
    ],
    "scribe": [
        "file_read", "file_write", "file_list", "grep_search",
    ],
    # v7.52 (maintainer): patcher must have ZERO tools so kimi cannot
    # reflexively call file_ops and must emit SEARCH/REPLACE prose blocks
    # (parsed by udiff_apply.py with fuzzy threshold 0.8). Without this entry
    # the default fallback ["file_read","file_list"] kicks in for the
    # agentic_loop path. See ai_bridge.py _CHAT_ONLY_ROLES for the parallel
    # guard on _execute_openai_compatible / _execute_litellm_tools paths.
    "patcher": [],
}


def get_tools_for_role(role: str) -> List[str]:
    """Get allowed tool names for a node role."""
    from node_roles import normalize_node_role
    normalized = normalize_node_role(role)
    return NODE_ROLE_TOOLS.get(normalized, ["file_read", "file_list"])


# ─── Agentic Loop ────────────────────────────────────────

class AgenticLoop:
    """
    Non-recursive state-machine agentic loop.

    Inspired by Claude Code's production-grade agent harness:
    - Uses while(True) loop with persistent state (no recursion/stack overflow)
    - StreamingToolExecutor for parallel tool execution
    - Three-level context compression
    - Circuit breaker for infinite loops
    """

    def __init__(
        self,
        config: LoopConfig,
        tool_registry: ToolRegistry,
        llm_call: Callable[..., Coroutine],
        on_event: Optional[Callable[[AgenticEvent], Coroutine]] = None,
    ) -> None:
        self.config = config
        self.tools = tool_registry
        self.llm_call = llm_call
        self.on_event = on_event

        self.context = ContextWindow(max_tokens=128_000)
        self.state = LoopState.INIT
        self.iteration = 0
        self.total_tool_calls = 0
        self.traces: List[ThinkingTrace] = []
        self.tool_call_stats: Dict[str, int] = {}
        self.files_modified: List[str] = []
        self.files_created: List[str] = []
        self.search_queries: List[str] = []
        self.start_time: float = 0.0

        # Circuit breaker: detect loops
        self._recent_actions: List[str] = []
        self._loop_detection_window = 6
        self._exhaustion_reason: Optional[str] = None  # Set by safety exits

    # v3.1: Tools that are safe to execute concurrently (read-only, no side effects).
    # Write tools (file_write, file_edit, bash) must run serially.
    _CONCURRENT_SAFE_TOOLS = frozenset({
        "file_read", "file_list", "glob", "grep_search", "multi_file_read",
        "web_search", "web_fetch",
    })

    # v6.1.6 (maintainer, OpenHands-inspired condensation)
    # When the loop hits a safety limit (max_iter/max_tool/loop_detected),
    # compress old tool_result bodies into short summaries before giving up.
    # If after condensation the model can still produce useful progress,
    # we keep going — matches OpenHands LLMSummarizingCondenser philosophy
    # (https://github.com/All-Hands-AI/OpenHands/tree/main/openhands/memory/condenser).
    _CONDENSATION_MAX_ATTEMPTS = 1  # one rescue per run — not unlimited

    def _try_condensation(self, reason: str) -> bool:
        """Compress tool_result bodies to buy one more iteration.

        Lightweight (no extra LLM call): keeps the LAST 4 tool_results verbatim;
        earlier tool_results shrink to a 200-char excerpt + tool name. Returns
        True if meaningful compression happened, False otherwise.
        """
        if getattr(self, "_condensation_attempts", 0) >= self._CONDENSATION_MAX_ATTEMPTS:
            return False
        messages = self.context.messages if hasattr(self.context, "messages") else None
        if not messages or len(messages) < 10:
            return False
        # Find tool-result messages (role="tool"). Keep last 4, shrink earlier.
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_indices) < 6:
            return False
        shrink_targets = tool_indices[:-4]
        bytes_saved = 0
        for idx in shrink_targets:
            msg = messages[idx]
            original = str(msg.get("content") or "")
            if len(original) <= 240:
                continue
            # Keep first 100 + ... + last 100 = 200 chars "receipt"
            compact = original[:100] + " … [condensed] … " + original[-100:]
            msg["content"] = compact
            bytes_saved += len(original) - len(compact)
        # Also shrink assistant narration if it's long
        for i, m in enumerate(messages):
            if m.get("role") == "assistant" and i < len(messages) - 4:
                c = str(m.get("content") or "")
                if len(c) > 400:
                    m["content"] = c[:200] + " … [condensed] … "
                    bytes_saved += len(c) - len(str(m["content"]))
        if bytes_saved < 500:
            return False
        self._condensation_attempts = getattr(self, "_condensation_attempts", 0) + 1
        # Opus R1 fix: keep ContextWindow.current_tokens consistent — other
        # compaction paths (snip/micro/nuclear) maintain this counter, so the
        # needs_compression heuristic stays accurate after condensation.
        if hasattr(self.context, "current_tokens"):
            approx_tokens_saved = bytes_saved // 4
            self.context.current_tokens = max(
                0, int(self.context.current_tokens) - approx_tokens_saved
            )
        logger.info(
            "Condensation applied: reason=%s bytes_saved=%d targets=%d node=%s",
            reason, bytes_saved, len(shrink_targets), self.config.node_key,
        )
        # Reset loop signatures so we don't immediately re-trigger loop_detected
        self._recent_actions = []
        return True

    async def _execute_single_tool(self, tc: "ToolCall") -> None:
        """Execute a single tool call and populate its result/error fields."""
        tc.started_at = time.time()
        tool = self.tools.get(tc.name)
        if tool:
            try:
                result = await asyncio.wait_for(
                    tool.execute(tc.arguments, {
                        "node_type": self.config.node_type,
                        "node_key": self.config.node_key,
                        "iteration": self.iteration,
                    }),
                    timeout=60.0,
                )
                if hasattr(result, "success"):
                    tc.metadata = dict(getattr(result, "metadata", {}) or {})
                    if getattr(result, "success", False):
                        tc.result = str(getattr(result, "output", "") or "")[:8000]
                    else:
                        tc.error = str(getattr(result, "error", "") or "Tool execution failed")[:500]
                        tc.result = f"Error: {tc.error}"
                else:
                    tc.result = str(result)[:8000]
            except asyncio.TimeoutError:
                tc.error = "Tool execution timed out (60s)"
                tc.result = tc.error
            except Exception as e:
                tc.error = str(e)[:500]
                tc.result = f"Error: {tc.error}"
        else:
            tc.result = f"Unknown tool: {tc.name}"
            tc.error = tc.result
        tc.completed_at = time.time()

    def _post_process_tool_call(self, tc: "ToolCall") -> None:
        """Update tracking state after a tool call completes."""
        self.tool_call_stats[tc.name] = self.tool_call_stats.get(tc.name, 0) + 1
        if tc.name in ("file_write", "file_edit") and not tc.error:
            path = tc.arguments.get("path", "") or tc.arguments.get("file_path", "")
            if path:
                action = str(tc.metadata.get("action", "")).lower()
                if action == "create" and path not in self.files_created:
                    self.files_created.append(path)
                elif tc.name == "file_write" and action != "overwrite" and path not in self.files_created:
                    self.files_created.append(path)
                elif path not in self.files_modified:
                    self.files_modified.append(path)
        if tc.name == "web_search" and not tc.error:
            query = tc.arguments.get("query", "")
            if query:
                self.search_queries.append(query)

    async def _emit(self, event_type: str, **data: Any) -> None:
        """Emit an event for real-time UI updates."""
        if self.on_event:
            event = AgenticEvent(
                timestamp=time.time(),
                event_type=event_type,
                data=data,
            )
            try:
                await self.on_event(event)
            except Exception:
                pass

    def _detect_loop(self, action_desc: str) -> bool:
        """Detect if the agent is stuck in a circular pattern.

        v3.1: Uses action_desc that includes tool name + argument hash to avoid
        false positives from legitimate alternating tool patterns (e.g., read A
        then read B is distinct from read A then read A).
        """
        self._recent_actions.append(action_desc)
        if len(self._recent_actions) > self._loop_detection_window * 2:
            self._recent_actions = self._recent_actions[-self._loop_detection_window * 2:]

        if len(self._recent_actions) < self._loop_detection_window:
            return False

        window = self._recent_actions[-self._loop_detection_window:]
        # v6.1.4 (maintainer): research showed reviewer repeat-screenshot
        # pattern didn't trigger prior 6-identical rule. Add: 4 consecutive
        # identical tool_calls in the window is a stuck signal — matches Cursor's
        # "Loop at most 3 times then ask" contract.
        tail4 = window[-4:]
        if len(tail4) == 4 and len(set(tail4)) == 1 and tail4[0].startswith("tool:"):
            logger.warning(
                "Loop detected (4 consecutive identical tool_calls): %s (node=%s)",
                tail4, self.config.node_key,
            )
            return True
        # Exact repetition: all entries in the window are identical (true stuck loop)
        if len(set(window)) == 1:
            logger.warning(
                "Loop detected (identical): %s (node=%s)",
                window[-3:], self.config.node_key,
            )
            return True
        # AB-AB pattern: window alternates between exactly 2 distinct actions
        if len(set(window)) == 2 and len(window) >= 4:
            # Check if it's truly alternating (not just 2 different tools used once each)
            half = len(window) // 2
            first_half = window[:half]
            second_half = window[half:]
            if first_half == second_half:
                logger.warning(
                    "Loop detected (repeating pattern): %s (node=%s)",
                    window[-4:], self.config.node_key,
                )
                return True
        return False

    async def run(
        self,
        system_prompt: str,
        user_input: str,
        handoff_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute the full agentic loop.

        Returns a result dict with:
          - success: bool
          - output: str (final text output)
          - tool_results: list of tool execution records
          - traces: list of thinking traces
          - files_modified: list of file paths
          - files_created: list of file paths
          - tool_call_stats: dict of tool_name -> call_count
          - usage: token usage dict
          - events: list of emitted events for replay
        """
        self.start_time = time.time()
        self.state = LoopState.INIT

        # Initialize context
        self.context.add_message("system", system_prompt)

        # Inject handoff context from upstream nodes
        if handoff_context:
            handoff_text = self._format_handoff_context(handoff_context)
            self.context.add_message("user", handoff_text)
            await self._emit("handoff_received", source=handoff_context.get("source_node", "unknown"))

        self.context.add_message("user", user_input)

        tool_defs = self.tools.get_tool_definitions(self.config.allowed_tools or None)
        all_tool_calls: List[ToolCall] = []
        final_output = ""
        accumulated_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # ── Main Loop (non-recursive state machine) ──
        self.state = LoopState.THINKING
        while self.state not in (LoopState.COMPLETED, LoopState.FAILED):
            self.iteration += 1

            # Safety: max iterations
            if self.iteration > self.config.max_iterations:
                if self._try_condensation("max_iterations"):
                    self.iteration -= 1  # give the rescued turn a fair shot
                    continue
                logger.warning("Agentic loop hit max iterations (%d)", self.config.max_iterations)
                self._exhaustion_reason = "max_iterations"
                self.state = LoopState.COMPLETED
                break

            # Safety: timeout
            elapsed = time.time() - self.start_time
            if elapsed > self.config.timeout_seconds:
                logger.warning("Agentic loop timeout after %.1fs", elapsed)
                self._exhaustion_reason = "timeout"
                self.state = LoopState.COMPLETED
                break

            # Safety: total tool calls
            if self.total_tool_calls > self.config.max_tool_calls:
                if self._try_condensation("max_tool_calls"):
                    continue
                logger.warning("Agentic loop hit max tool calls (%d)", self.config.max_tool_calls)
                self._exhaustion_reason = "max_tool_calls"
                self.state = LoopState.COMPLETED
                break

            # ── THINK: Call LLM ──
            await self._emit("thinking", iteration=self.iteration, state=str(self.state))

            try:
                response = await self.llm_call(
                    messages=self.context.get_messages(),
                    tools=tool_defs if tool_defs else None,
                    tool_choice="auto" if tool_defs else None,
                )
            except Exception as e:
                logger.error("LLM call failed in agentic loop: %s", str(e)[:200])
                self.state = LoopState.FAILED
                final_output = f"LLM call failed: {str(e)[:500]}"
                break

            # Extract response
            message = response.get("message", {})
            content = str(message.get("content") or "").strip()
            tool_calls_raw = message.get("tool_calls") or []
            usage = response.get("usage", {})

            # Accumulate usage (billing/tracking only — NOT used for context window sizing)
            accumulated_usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            accumulated_usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            accumulated_usage["total_tokens"] += int(usage.get("total_tokens") or 0)
            # v3.0 fix: Do NOT overwrite context.current_tokens with cumulative billing total.
            # context.current_tokens tracks local context window occupancy (chars÷4 approx).
            # accumulated_usage tracks total API usage across ALL iterations (always growing).
            # Overwriting confused compression thresholds (needs_compression could fire early/late).
            # Instead, add only the NEW tokens from this response to the context estimate.
            new_content = content + "".join(
                str(tc.get("function", {}).get("arguments", "")) for tc in tool_calls_raw
            )
            self.context.current_tokens += ContextWindow._estimate_tokens(new_content)

            # Record trace
            trace = ThinkingTrace(
                timestamp=time.time(),
                state=self.state,
                summary=content[:200] if content else "(tool calls)",
                token_count=accumulated_usage["total_tokens"],
                context_percent=self.context.usage_percent,
            )

            if content:
                final_output = content
                await self._emit("text_chunk", text=content[:500])

            # ── ACT: Execute tools if present ──
            if tool_calls_raw:
                self.state = LoopState.ACTING
                # Add assistant message with tool calls to context
                # v3.0.5 FIX: Removed duplicate tc_args token counting here.
                # Tool-call arguments are already counted in new_content (line ~641-644).
                self.context.messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls_raw,
                })

                iteration_tool_calls = []
                for tc_raw in tool_calls_raw:
                    tc = ToolCall(
                        id=tc_raw.get("id", f"tc_{self.total_tool_calls}"),
                        name=tc_raw.get("function", {}).get("name", ""),
                        arguments=self._parse_tool_args(tc_raw.get("function", {}).get("arguments", "{}")),
                    )
                    iteration_tool_calls.append(tc)

                # v3.1: Parallel tool execution for concurrent-safe tools.
                # Partition into read-only (concurrent) and write (serial) groups.
                # Concurrent-safe tools run in parallel via asyncio.gather.
                concurrent_batch: List[ToolCall] = []
                serial_queue: List[ToolCall] = []
                for tc in iteration_tool_calls:
                    if tc.name in self._CONCURRENT_SAFE_TOOLS:
                        concurrent_batch.append(tc)
                    else:
                        serial_queue.append(tc)

                async def _run_and_record(tc: ToolCall) -> bool:
                    """Run one tool with safety checks. Returns False to stop."""
                    # v3.0.5 FIX: Include argument hash so read("a.py") ≠ read("b.py")
                    _args_hash = hashlib.md5(str(tc.arguments).encode()).hexdigest()[:8]
                    if self._detect_loop(f"tool:{tc.name}:{_args_hash}"):
                        if self._try_condensation("loop_detected"):
                            # Skip this exact repeat but let the next iteration run
                            return True
                        self._exhaustion_reason = "loop_detected"
                        self.state = LoopState.COMPLETED
                        return False
                    if self.total_tool_calls >= self.config.max_tool_calls:
                        logger.warning("Agentic loop hit max tool calls (%d) during iteration",
                                       self.config.max_tool_calls)
                        self._exhaustion_reason = "max_tool_calls"
                        self.state = LoopState.COMPLETED
                        return False
                    self.total_tool_calls += 1
                    await self._emit("tool_start", tool=tc.name, args_preview=str(tc.arguments)[:200])
                    await self._execute_single_tool(tc)
                    await self._emit("tool_end", tool=tc.name, duration_ms=round(tc.duration_ms),
                                     success=tc.error is None, result_preview=str(tc.result or "")[:200])
                    self._post_process_tool_call(tc)
                    self.context.add_tool_result(tc.id, tc.result or "")
                    all_tool_calls.append(tc)
                    return True

                # Phase 1: Run concurrent-safe tools in parallel (Claude Code
                # partitionToolCalls + runConcurrently style). Reads like
                # file_read / glob / grep_search all execute simultaneously.
                # v6.1.8 Wave C (maintainer): observed latency for this
                # path to confirm the concurrency win is actually happening.
                if concurrent_batch and self.state not in (LoopState.COMPLETED, LoopState.FAILED):
                    # Pre-check: how many can we still run?
                    budget = self.config.max_tool_calls - self.total_tool_calls
                    runnable = concurrent_batch[:budget] if budget < len(concurrent_batch) else concurrent_batch
                    if runnable:
                        self.total_tool_calls += len(runnable)
                        for tc in runnable:
                            await self._emit("tool_start", tool=tc.name, args_preview=str(tc.arguments)[:200])
                        _phase1_t0 = time.time()
                        await asyncio.gather(*(self._execute_single_tool(tc) for tc in runnable))
                        _phase1_dt = (time.time() - _phase1_t0) * 1000.0
                        if len(runnable) >= 2:
                            _seq_est = sum(tc.duration_ms for tc in runnable)
                            _saved = max(0, _seq_est - _phase1_dt)
                            logger.info(
                                "concurrent_tools: ran=%d parallel=%dms sequential_est=%dms saved=%dms node=%s",
                                len(runnable), int(_phase1_dt), int(_seq_est),
                                int(_saved), self.config.node_key,
                            )
                        for tc in runnable:
                            await self._emit("tool_end", tool=tc.name, duration_ms=round(tc.duration_ms),
                                             success=tc.error is None, result_preview=str(tc.result or "")[:200])
                            self._post_process_tool_call(tc)
                            self.context.add_tool_result(tc.id, tc.result or "")
                            all_tool_calls.append(tc)
                    if len(runnable) < len(concurrent_batch):
                        self._exhaustion_reason = "max_tool_calls"
                        self.state = LoopState.COMPLETED

                # Phase 2: Run serial tools one by one (with per-tool safety checks)
                for tc in serial_queue:
                    if self.state in (LoopState.COMPLETED, LoopState.FAILED):
                        break
                    if not await _run_and_record(tc):
                        break

                trace.tool_calls = iteration_tool_calls
                self.traces.append(trace)

                if self.state in (LoopState.COMPLETED, LoopState.FAILED):
                    continue

                # ── OBSERVE: Check for context_compress signal ──
                force_compress = False
                compress_level = "auto"
                for tc in iteration_tool_calls:
                    if tc.name == "context_compress" and not tc.error:
                        force_compress = True
                        meta = tc.arguments if isinstance(tc.arguments, dict) else {}
                        compress_level = str(meta.get("level", "auto")).upper()
                        break

                # ── OBSERVE: Check if context needs compression ──
                if self.config.enable_context_compression and (
                    self.context.needs_compression or force_compress
                ):
                    self.state = LoopState.COMPRESSING
                    await self._emit("context_compress", usage_percent=round(self.context.usage_percent),
                                     forced=force_compress, level=compress_level)

                    # v3.1: 3-level compression cascade. Explicit L3 skips L1/L2
                    # since full_collapse replaces everything anyway.
                    saved = 0
                    if compress_level == "L3":
                        saved += self.context.full_collapse()
                    else:
                        if compress_level in ("AUTO", "L1"):
                            saved += self.context.snip_compact()
                        if compress_level in ("AUTO", "L2") and (self.context.needs_compression or force_compress):
                            saved += self.context.micro_compact()
                        if compress_level == "AUTO" and (self.context.needs_compression or force_compress):
                            saved += self.context.full_collapse()

                    if saved > 0:
                        await self._emit("context_compressed", tokens_saved=saved,
                                         level="L3" if self.context.full_collapse_count > 0 else
                                               ("L2" if self.context.micro_compact_count > 0 else "L1"))

                # Continue loop — go back to THINKING
                if self.state not in (LoopState.COMPLETED, LoopState.FAILED):
                    self.state = LoopState.THINKING

            else:
                # No tool calls — the model has completed
                self.traces.append(trace)
                self.state = LoopState.COMPLETED

        # ── Build result ──
        await self._emit("complete", iterations=self.iteration, total_tool_calls=self.total_tool_calls)

        # success is True ONLY if the loop completed naturally (model stopped calling tools).
        # Safety-exit (max_iterations, timeout, max_tool_calls) yields success=False
        # with a distinct exhaustion_reason so callers can distinguish from hard failures.
        exhausted = getattr(self, "_exhaustion_reason", None)
        return {
            "success": self.state == LoopState.COMPLETED and not exhausted,
            "exhausted": bool(exhausted),
            "exhaustion_reason": exhausted or "",
            "output": final_output,
            "tool_results": [
                {
                    "tool": tc.name,
                    "args": tc.arguments,
                    "result": tc.result,
                    "metadata": tc.metadata,
                    "error": tc.error,
                    "started_at": tc.started_at,
                    "completed_at": tc.completed_at,
                    "duration_ms": round(tc.duration_ms),
                }
                for tc in all_tool_calls
            ],
            "traces": [
                {
                    "ts": t.timestamp,
                    "state": str(t.state),
                    "summary": t.summary,
                    "tools": [tc.name for tc in t.tool_calls],
                    "tokens": t.token_count,
                    "context_pct": round(t.context_percent, 1),
                }
                for t in self.traces
            ],
            "files_modified": list(set(self.files_modified)),
            "files_created": list(set(self.files_created)),
            "search_queries": self.search_queries,
            "tool_call_stats": dict(self.tool_call_stats),
            "usage": accumulated_usage,
            "iterations": self.iteration,
            "duration_seconds": round(time.time() - self.start_time, 1),
            "context_compressions": {
                "snip": self.context.snip_count,
                "micro": self.context.micro_compact_count,
                "full": self.context.full_collapse_count,
            },
        }

    def _format_handoff_context(self, handoff: Dict[str, Any]) -> str:
        """Format upstream node handoff context into a readable message."""
        parts = []
        source = handoff.get("source_node", "unknown")
        parts.append(f"## Handoff from {source}")

        if handoff.get("context_summary"):
            parts.append(f"\n### Context Summary\n{handoff['context_summary']}")

        if handoff.get("files_produced"):
            parts.append("\n### Files Produced")
            for f in handoff["files_produced"]:
                parts.append(f"- `{f}`")

        if handoff.get("decisions_made"):
            parts.append("\n### Key Decisions")
            for d in handoff["decisions_made"]:
                parts.append(f"- {d}")

        if handoff.get("open_questions"):
            parts.append("\n### Open Questions")
            for q in handoff["open_questions"]:
                parts.append(f"- {q}")

        if handoff.get("source_bundles"):
            parts.append("\n### Reference Source Code")
            for bundle in handoff["source_bundles"][:5]:
                parts.append(f"\n#### {bundle.get('title', 'Reference')}")
                parts.append(f"```\n{str(bundle.get('content', ''))[:3000]}\n```")

        return "\n".join(parts)

    def _parse_tool_args(self, raw: str | Dict) -> Dict[str, Any]:
        """Safely parse tool arguments from LLM output."""
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(str(raw))
        except (json.JSONDecodeError, TypeError):
            return {"raw": str(raw)}


# ─── Connection Pool Manager ─────────────────────────────

class ConnectionPoolManager:
    """
    Persistent HTTP connection pool for API calls.

    Eliminates TCP handshake + TLS negotiation overhead on repeated calls
    to the same provider. Each provider gets its own pool with keepalive.
    """

    _instance: Optional["ConnectionPoolManager"] = None
    _lock = asyncio.Lock() if hasattr(asyncio, 'Lock') else None

    def __init__(self) -> None:
        self._pools: Dict[str, Any] = {}
        self._initialized = False

    @classmethod
    async def get_instance(cls) -> "ConnectionPoolManager":
        # v3.1: Guard with lock to prevent TOCTOU race where two coroutines
        # both see _instance is None and create duplicate singletons.
        if cls._instance is None:
            if cls._lock is None:
                cls._lock = asyncio.Lock()
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    async def get_session(self, provider: str, base_url: str = "") -> Any:
        """Get or create a persistent aiohttp session for a provider."""
        import aiohttp

        pool_key = f"{provider}:{base_url}" if base_url else provider

        if pool_key not in self._pools or self._pools[pool_key].closed:
            connector = aiohttp.TCPConnector(
                limit=500,              # was 10 — openai-python/LiteLLM use 1000
                limit_per_host=100,     # per-host cap (new, from LiteLLM)
                ttl_dns_cache=300,
                keepalive_timeout=120,  # was 60 — LiteLLM uses 120
                enable_cleanup_closed=True,
                force_close=False,
            )
            timeout = aiohttp.ClientTimeout(
                total=600,
                connect=5,              # was 10 — openai-python uses 5
                sock_connect=5,         # was 10
                sock_read=300,
            )
            self._pools[pool_key] = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    "Connection": "keep-alive",
                    "Accept": "application/json",
                },
            )
            logger.info("Created connection pool for provider=%s base=%s", provider, base_url[:50])
        return self._pools[pool_key]

    async def close_all(self) -> None:
        """Close all connection pools."""
        for key, session in list(self._pools.items()):
            try:
                await session.close()
            except Exception:
                pass
        self._pools.clear()


# ─── Optimized Retry Strategy ────────────────────────────

class RetryStrategy:
    """
    Optimized retry logic inspired by Cursor and Claude Code.

    Key improvements over v2.0:
    - 429: Read Retry-After header for precise wait
    - Non-429: First retry at 500ms (was 2s)
    - Circuit breaker: 3 consecutive failures → fast model switch
    """

    def __init__(self) -> None:
        self._failure_counts: Dict[str, int] = {}
        self._circuit_open: Dict[str, float] = {}
        self.circuit_breaker_threshold = 3
        self.circuit_reset_seconds = 120.0

    def get_wait_time(
        self,
        attempt: int,
        error_message: str = "",
        retry_after_header: Optional[float] = None,
    ) -> float:
        """Calculate optimal wait time for retry."""
        if retry_after_header and retry_after_header > 0:
            return min(retry_after_header, 30.0)

        is_rate_limit = any(
            kw in str(error_message).lower()
            for kw in ("429", "rate limit", "too many requests", "quota")
        )

        if is_rate_limit:
            # Rate limit: wait longer but with jitter
            import random
            return min(8.0 * (1.5 ** (attempt - 1)) + random.uniform(0, 2), 60.0)
        else:
            # Other errors: fast first retry, then exponential
            import random
            if attempt <= 1:
                return 0.5 + random.uniform(0, 0.5)
            return min(2.0 ** attempt + random.uniform(0, 1.5), 30.0)

    def record_failure(self, provider: str) -> None:
        self._failure_counts[provider] = self._failure_counts.get(provider, 0) + 1
        if self._failure_counts[provider] >= self.circuit_breaker_threshold:
            self._circuit_open[provider] = time.time()
            logger.warning("Circuit breaker OPEN for provider=%s after %d failures",
                           provider, self._failure_counts[provider])

    def record_success(self, provider: str) -> None:
        self._failure_counts.pop(provider, None)
        self._circuit_open.pop(provider, None)

    def is_circuit_open(self, provider: str) -> bool:
        if provider not in self._circuit_open:
            return False
        elapsed = time.time() - self._circuit_open[provider]
        if elapsed > self.circuit_reset_seconds:
            # Half-open: allow one attempt
            self._circuit_open.pop(provider, None)
            self._failure_counts.pop(provider, None)
            return False
        return True


# ─── Global instances ────────────────────────────────────

_global_retry_strategy = RetryStrategy()
_global_tool_registry = ToolRegistry()



def get_retry_strategy() -> RetryStrategy:
    return _global_retry_strategy


def get_tool_registry() -> ToolRegistry:
    return _global_tool_registry


# ─── Function-based Tool Adapter ─────────────────────────

class FunctionAgenticTool(AgenticTool):
    """Wraps an async function from agentic_tools.py as an AgenticTool."""

    def __init__(self, name: str, func, definition: Dict[str, Any]) -> None:
        self.name = name
        self._func = func
        self.description = definition.get("description", "")
        self.parameters = definition.get("parameters", {})
        self._definition = definition

    def get_definition(self) -> Dict[str, Any]:
        return dict(self._definition)

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Any:
        try:
            return await self._func(**arguments)
        except Exception as e:
            raise RuntimeError(f"Tool error: {str(e)[:500]}") from e


def register_default_tools() -> int:
    """Register concrete tool implementations from agentic_tools.py into the global registry.

    Returns the number of tools registered.
    """
    try:
        from agentic_tools import TOOL_REGISTRY, TOOL_DEFINITIONS
    except ImportError:
        logger.warning("agentic_tools not available; skipping tool registration")
        return 0

    registered = 0
    for name, func in TOOL_REGISTRY.items():
        if name in TOOL_DEFINITIONS and name not in {t.name for t in _global_tool_registry.list_tools()}:
            tool = FunctionAgenticTool(name, func, TOOL_DEFINITIONS[name])
            _global_tool_registry.register(tool)
            registered += 1
    if registered:
        logger.info("Registered %d agentic tools into global ToolRegistry", registered)
    return registered


# Auto-register tools on import
try:
    register_default_tools()
except Exception:
    pass
