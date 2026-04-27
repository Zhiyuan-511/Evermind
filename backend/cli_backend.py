"""
Evermind Backend — CLI Backend Engine
Routes node execution to local AI CLI tools (Claude Code, Codex, Gemini CLI, Aider, etc.)
instead of going through API relay endpoints.

Architecture inspired by:
- MCO (github.com/mco-org/mco) — shim transport / subprocess spawning
- ComposioHQ (github.com/ComposioHQ/agent-orchestrator) — Runtime abstraction interface
- Claude Agent SDK — programmatic Claude Code access
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("evermind.cli_backend")

# ─────────────────────────────────────────────
# CLI Registry — supported CLI tools and their invocation patterns
# ─────────────────────────────────────────────

@dataclass
class CLIProfile:
    """Describes how to invoke and parse output from a specific CLI tool."""
    name: str
    display_name: str
    binary_names: List[str]           # Possible binary names to search for
    version_cmd: List[str]            # Command to check version
    build_cmd: Any = None             # Callable(task, workspace, timeout) -> List[str]
    parse_output: Any = None          # Callable(stdout, stderr, returncode) -> Dict
    supports_json: bool = False       # Native JSON output support
    supports_streaming: bool = False  # Can stream progress
    supports_file_ops: bool = False   # Can read/write files directly
    max_timeout: int = 600            # Default max timeout in seconds
    detected_path: Optional[str] = None
    detected_version: Optional[str] = None


def _build_claude_cmd(task: str, workspace: str, timeout: int, **kwargs) -> List[str]:
    """Build Claude Code CLI command.

    v7.1g (maintainer 2026-04-24): per-task session reuse + cache-friendly prompt.
    KEY upgrades over v7.1c:
      1. --session-id (UUID derived from task hash): same task across multiple
         orchestrator iterations (e.g. patcher round 1, 2, 3 of same lane)
         shares prompt cache → input token ↓ 20-30% on rounds 2+, latency ↓.
      2. --append-system-prompt instead of stuffing everything into the user
         task: Claude's DEFAULT system prompt (TodoWrite/Task/Skills/etc
         agentic capabilities) becomes the stable cache prefix; our additions
         are appended, growing the cache without breaking it.
      3. --exclude-dynamic-system-prompt-sections: moves per-machine sections
         (cwd, env, memory paths, git status) out of system → first user msg.
         Cross-process / cross-orchestrator-restart cache hit ↑ 30-50%.
      4. --include-partial-messages + stream-json (when caller opts in): UX
         signal for early progress detection.
    """
    ultra_on = bool(kwargs.get("ultra_mode"))
    node_type = (kwargs.get("node_type", "") or "").strip().lower()
    # v7.1i (maintainer 2026-04-25 from Aider/OpenHands research):
    # Per-node-type max-turns (was statically 300/80). Patcher / polisher
    # / merger don't need 300 turns — capping them prevents runaway loops
    # that drain Claude weekly quota on essentially trivial work.
    _NODE_TURN_CAPS = {
        "router": "20", "planner": "60", "analyst": "120",
        "uidesign": "80", "scribe": "80",
        "builder": "300",
        "merger": "40", "polisher": "30", "patcher": "30",
        "reviewer": "100", "tester": "60", "debugger": "120", "deployer": "20",
    }
    if ultra_on:
        max_turns = _NODE_TURN_CAPS.get(node_type, "300")
    else:
        max_turns = "80"
    # v7.1i: per-node tool restriction. planner/analyst/reviewer don't need
    # Write/Edit/Bash; patcher doesn't need WebFetch. Smaller tool schema =
    # more stable cache prefix + fewer mis-invocations.
    _NODE_TOOLS = {
        "router":   "Read,Grep,Glob",
        "planner":  "Read,Grep,Glob,WebFetch,WebSearch,TodoWrite",
        "analyst":  "Read,Grep,Glob,WebFetch,WebSearch,TodoWrite,Task",
        "uidesign": "Read,Edit,Write,Glob,WebFetch",
        "scribe":   "Read,Edit,Write,Glob,WebFetch,WebSearch",
        "reviewer": "Read,Bash,Glob,Grep,WebFetch",
        "tester":   "Read,Bash,Glob,Grep",
        "patcher":  "Read,Edit,Write,Glob,Grep,Bash",
    }
    full_tools = _NODE_TOOLS.get(
        node_type,
        "Read,Edit,Write,Bash,Glob,Grep,WebFetch,WebSearch,TodoWrite,Task,NotebookEdit",
    )
    cmd = [
        "claude",
        "-p", task,
        "--output-format", "json",
        "--allowedTools", full_tools,
        "--max-turns", max_turns,
        "--dangerously-skip-permissions",
        # v7.1g: cross-process cache friendliness — only works with default
        # system prompt (we use --append-system-prompt for additions, NOT
        # --system-prompt which would break this flag's semantics).
        "--exclude-dynamic-system-prompt-sections",
    ]
    # Stable session-id (UUID5 from node identity) for cache reuse across
    # iterations. UUID5 is deterministic so retries hit the same session.
    session_seed = str(kwargs.get("session_seed", "") or "").strip()
    if session_seed:
        try:
            import uuid as _uuid
            session_uuid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, session_seed))
            cmd.extend(["--session-id", session_uuid])
        except Exception:
            pass
    # CLI-specialization / lane-identity → append to default system prompt.
    # This is the cache-friendly way to add custom instructions.
    append_system = str(kwargs.get("append_system_prompt", "") or "").strip()
    if append_system:
        cmd.extend(["--append-system-prompt", append_system])
    model = kwargs.get("model", "")
    if model:
        cmd.extend(["--model", model])
    if workspace:
        cmd.extend(["--add-dir", workspace])
    return cmd


def _parse_claude_output(stdout: str, stderr: str, returncode: int) -> Dict:
    """Parse Claude Code JSON output into standard result dict.

    Non-bare mode with --output-format json returns a single JSON object:
    {type: "result", subtype: "success", is_error: bool, result: str, total_cost_usd: float, usage: {...}}
    """
    if returncode != 0 and not stdout.strip():
        return {
            "success": False,
            "output": "",
            "error": f"Claude Code exited with code {returncode}: {stderr[:500]}",
        }
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            # Standard non-bare JSON output: {type: "result", result: "...", is_error: bool}
            is_error = data.get("is_error", False)
            result_text = data.get("result", "")
            cost = data.get("total_cost_usd", 0)
            usage = data.get("usage", {})
            return {
                "success": not is_error,
                "output": result_text,
                "error": result_text if is_error else "",
                "files_created": [],
                "raw_response": data,
                "cost": cost,
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                },
            }
        elif isinstance(data, list):
            # Older format: array of content blocks
            text_parts = []
            files_created = []
            for block in data:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        tool_name = block.get("tool_name", "")
                        if tool_name in ("Write", "Edit"):
                            path = block.get("input", {}).get("file_path", "")
                            if path:
                                files_created.append(path)
            return {
                "success": True,
                "output": "\n".join(text_parts),
                "files_created": files_created,
                "raw_response": data,
            }
    except json.JSONDecodeError:
        pass
    # Fallback: treat entire stdout as text output
    return {
        "success": returncode == 0,
        "output": stdout.strip(),
        "files_created": [],
    }


def _build_codex_cmd(task: str, workspace: str, timeout: int, **kwargs) -> List[str]:
    """Build Codex CLI command.

    v7.1g (maintainer 2026-04-24): per-node-type profile selection. Codex
    config.toml has `[profiles.evermind-<role>]` blocks tuning
    `model_reasoning_effort` per node type — planner/reviewer="high",
    builder/patcher="medium", merger="minimal", polisher="low".
    Latency scales roughly linearly with reasoning effort, so this alone
    cuts builder/merger latency 50-80%.

    Also adds:
      --ephemeral: don't persist session to ~/.codex/sessions (concurrent-safe)
      --ignore-rules: don't load .rules files (clean state)
      --output-schema: when provided, force structured JSON output
                       (kills narration "I created file X..." → token ↓ 40-70%)
    """
    ultra_on = bool(kwargs.get("ultra_mode"))
    node_type = (kwargs.get("node_type", "") or "").strip().lower()
    cmd = [
        "codex", "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",       # no session persistence → concurrent-safe
        "--ignore-rules",    # no .rules files → clean state
    ]
    # Sandbox / approval control
    if ultra_on:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.append("--full-auto")
    # Per-node profile (config.toml [profiles.evermind-<role>])
    _profile_map = {
        "planner": "evermind-planner",
        "analyst": "evermind-planner",
        "uidesign": "evermind-planner",
        "scribe": "evermind-builder",
        "builder": "evermind-builder",
        "merger": "evermind-merger",
        "polisher": "evermind-polisher",
        "reviewer": "evermind-reviewer",
        "patcher": "evermind-patcher",
        "debugger": "evermind-patcher",
        "deployer": "evermind-merger",
        "tester": "evermind-reviewer",
    }
    profile_name = _profile_map.get(node_type, "")
    if profile_name:
        cmd.extend(["-p", profile_name])
    # GMN relay env-key wiring (always inject base_url since profile only sets provider name)
    # v7.1i (2026-04-25):
    #  - base_url 从 settings.cli_relay_keys.codex.base_url 读取（默认
    #    https://relayx.com，可在 UI 改）。旧硬编码 relay.cn/v1 已下线。
    #  - 没填 key（订阅模式或未配置）→ 跳过整段 -c 注入，让 codex 走自带的
    #    ChatGPT OAuth (~/.codex/auth.json)。
    if os.getenv("GMN_OPENAI_API_KEY"):
        _codex_base_url = "https://relayx.com"
        try:
            from settings import load_settings as _load_settings  # local import (avoid cycle at module load)
            _s = _load_settings() or {}
            _codex_cfg = ((_s.get("cli_relay_keys") or {}).get("codex") or {})
            _user_url = (_codex_cfg.get("base_url") or "").strip()
            if _user_url:
                _codex_base_url = _user_url
        except Exception:
            pass
        cmd.extend([
            "-c", 'model_providers.gmn.name="GMN Code"',
            "-c", f'model_providers.gmn.base_url="{_codex_base_url}"',
            "-c", "model_providers.gmn.env_key=GMN_OPENAI_API_KEY",
            "-c", "model_providers.gmn.wire_api=responses",
        ])
    # Optional: output-schema for builder lanes — kills narration
    output_schema_path = kwargs.get("output_schema_path", "")
    if output_schema_path and os.path.exists(output_schema_path):
        cmd.extend(["--output-schema", output_schema_path])
    # Explicit model override beats profile
    model = kwargs.get("model", "")
    if model:
        cmd.extend(["-m", model])
    if workspace:
        cmd.extend(["-C", workspace])
        cmd.extend(["--add-dir", workspace])
    cmd.append(task)
    return cmd


def _parse_codex_output(stdout: str, stderr: str, returncode: int) -> Dict:
    """Parse Codex CLI JSONL output.

    Codex --json outputs JSONL events:
      {type: "thread.started", thread_id: ...}
      {type: "turn.started"}
      {type: "item.completed", item: {type: "agent_message", text: "..."}}
      {type: "item.completed", item: {type: "tool_call", ...}}
      {type: "turn.completed"}
      {type: "thread.completed", ...}
    We extract text from agent_message items.
    """
    if returncode != 0 and not stdout.strip():
        return {
            "success": False,
            "output": "",
            "error": f"Codex exited with code {returncode}: {stderr[:500]}",
        }

    text_parts = []
    files_changed = []
    last_event = None

    for line in stdout.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            last_event = obj
            if not isinstance(obj, dict):
                continue
            evt_type = obj.get("type", "")

            # Extract text from completed agent messages
            if evt_type == "item.completed":
                item = obj.get("item", {})
                if isinstance(item, dict):
                    if item.get("type") == "agent_message":
                        text = item.get("text", "")
                        if text:
                            text_parts.append(text)
                    elif item.get("type") == "tool_call":
                        # Track file operations
                        fn = item.get("name", "")
                        if fn in ("write_file", "create_file", "edit_file"):
                            fpath = (item.get("arguments", {}) or {}).get("path", "")
                            if fpath:
                                files_changed.append(fpath)

            # thread.completed may have summary info
            elif evt_type == "thread.completed":
                summary = obj.get("summary", "")
                if summary and not text_parts:
                    text_parts.append(summary)

        except json.JSONDecodeError:
            # Not JSON — append as raw text
            text_parts.append(line)

    output = "\n".join(t for t in text_parts if t)
    return {
        "success": returncode == 0 and bool(output),
        "output": output,
        "files_created": files_changed,
        "raw_response": last_event,
    }


def _build_gemini_cmd(task: str, workspace: str, timeout: int, **kwargs) -> List[str]:
    """Build Gemini CLI command.

    v7.1d (maintainer 2026-04-24): 解放 Gemini 全部能力。Gemini CLI 没有
    Claude 的 --max-turns，但通过 --approval-mode=yolo 可以让模型一直
    跑工具直到任务完成（默认会停在第一个交互点）。Ultra 时切换到
    Gemini 3.1 Pro Preview 默认（最强），普通模式 2.5 Pro。

    v7.1e: Gemini CLI hard-fails (exit 1, ~3s) when `.geminiignore` is
    missing in workspace AND `--include-directories` is set. Pre-create
    an empty file to satisfy that check — this was the silent ~3s fail
    we saw in run_b318e114b689 where every Gemini call fell back to
    Claude. Cost us the user's intended Gemini-frontend / Claude-backend
    split for the first 2-3 nodes.

    Ref:
      gemini -p <task> --approval-mode yolo -o json -m gemini-3.1-pro-preview
        --include-directories <workspace>
    """
    ultra_on = bool(kwargs.get("ultra_mode"))
    # v7.1i (maintainer 2026-04-25 — final): Pre-create .geminiignore with REAL
    # content (not empty). Empty .geminiignore + --include-directories=Desktop
    # caused MemoryDiscovery to scan node_modules / git history / large media
    # files → server-side 429 retry storm → CPU 0% deadlock.
    # GitHub issues #22635, #13192 confirm this. Content below tells gemini
    # to skip the bulk-noise paths.
    _GEMINI_IGNORE_CONTENT = """\
node_modules/
.git/
.next/
.venv/
__pycache__/
*.log
*.tmp
*.cache
*.png
*.jpg
*.jpeg
*.gif
*.mp4
*.mov
*.zip
*.tar.gz
.DS_Store
"""
    try:
        for _dir in ({os.getcwd(), workspace} - {""}):
            if _dir and os.path.isdir(_dir):
                _ig = os.path.join(_dir, ".geminiignore")
                # Always rewrite — older runs may have left empty file.
                try:
                    with open(_ig, "w", encoding="utf-8") as _fh:
                        _fh.write(_GEMINI_IGNORE_CONTENT)
                except Exception:
                    pass
    except Exception:
        pass
    # v7.1i: trust workspace via env (replaces `--skip-trust` flag which 0.39
    # marks as unsupported and prints warning).
    os.environ["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    # v7.1i (final): subprocess hardening per gemini-cli GitHub issues.
    #  • GEMINI_CLI_NO_RELAUNCH=1 prevents auto-relaunch loop on TTY detect.
    #  • Drop GEMINI_CLI_IDE_* — issue #12362: headless mode hangs trying to
    #    connect to IDE companion via SSE when these env vars are set.
    #  • GEMINI_TELEMETRY_DISABLED=1 stops background tower analytics that
    #    can hang on first-run.
    os.environ["GEMINI_CLI_NO_RELAUNCH"] = "1"
    os.environ["GEMINI_TELEMETRY_DISABLED"] = "1"
    for _bad_env in (
        "GEMINI_CLI_IDE_WORKSPACE_PATH",
        "GEMINI_CLI_IDE_SERVER_PORT",
        "GEMINI_CLI_IDE_PID",
    ):
        os.environ.pop(_bad_env, None)
    # v7.1i (final): write .gemini/settings.json to cap thinkingBudget +
    # maxAttempts. Default thinkingBudget=-1 (dynamic) can spiral up to
    # 24576 tokens of thinking → server-side timeout. Cap at 8192. Disable
    # automatic retry on fetch errors so we surface failures fast (instead
    # of CLI silently retrying for 5+ minutes).
    try:
        _settings_dir = os.path.expanduser("~/.gemini")
        _settings_path = os.path.join(_settings_dir, "settings.json")
        if not os.path.exists(_settings_path):
            os.makedirs(_settings_dir, exist_ok=True)
            import json as _json
            _settings_payload = {
                "general": {"maxAttempts": 3, "retryFetchErrors": False},
                "modelConfigs": {
                    "aliases": {
                        "default": {
                            "modelConfig": {
                                "generateContentConfig": {
                                    "thinkingConfig": {"thinkingBudget": 8192}
                                }
                            }
                        }
                    }
                },
            }
            with open(_settings_path, "w", encoding="utf-8") as _sf:
                _json.dump(_settings_payload, _sf, indent=2)
    except Exception:
        pass
    cmd = [
        "gemini",
        "-p", task,
        "--approval-mode", "yolo",
        "-o", "json",
    ]
    # v7.1i (maintainer 2026-04-25 — final v4): use gemini-2.5-pro (GA stable).
    # ROOT CAUSE confirmed via direct CLI reproduction with exact stderr:
    #   {"error":{"message":"No capacity available for model
    #     gemini-3.1-pro-preview on the server","code":1}}
    # gemini-3.1-pro-preview is a Google PREVIEW channel with shared limited
    # capacity — long/complex prompts get rejected within 15s. Official
    # subscription DOES NOT cover preview-channel capacity (preview is
    # explicitly out of SLA per Google docs). Using the model only works
    # for trivial requests.
    # gemini-2.5-pro is GA, sub-5% quality gap on coding, full subscription
    # SLA capacity guarantee. Caller can opt back into preview via
    # kwargs["model"]="gemini-3.1-pro-preview" when Google promotes it to GA.
    model = kwargs.get("model", "")
    if not model:
        model = "gemini-2.5-pro"
    if model:
        cmd.extend(["-m", model])
    # v7.1i (final): only --include-directories on safe workspaces.
    # If workspace is a "scary big" dir like ~/Desktop or ~ — the
    # MemoryDiscovery pass scans EVERY file (issue #22635 #13192).
    # Output dir / build artifact dirs are fine.
    DANGEROUS_DIRS = {
        os.path.expanduser("~/Desktop"),
        os.path.expanduser("~/Documents"),
        os.path.expanduser("~"),
        "/",
    }
    if workspace and os.path.isdir(workspace):
        try:
            _real = os.path.realpath(workspace)
            if _real not in DANGEROUS_DIRS:
                cmd.extend(["--include-directories", workspace])
        except Exception:
            pass
    return cmd


def _parse_gemini_output(stdout: str, stderr: str, returncode: int) -> Dict:
    """Parse Gemini CLI -o json output.

    v7.1d: Real-world output structure (verified by smoke test 2026-04-24):
        {
          "session_id": "...",
          "response": "...",        # ← 主要答复内容
          "stats": {
            "models": {
              "gemini-3.1-pro-preview": {
                "tokens": {"prompt": N, "candidates": N, "total": N, ...},
                "api": {"totalLatencyMs": N, ...}
              }
            },
            "tools": {"totalCalls": N, "byName": {...}, "totalDecisions": {...}}
          }
        }
    There may be a leading non-JSON header (YOLO mode banners, MCP server
    init logs), so we extract the first '{' through the matching '}'.
    """
    if returncode != 0 and not stdout.strip():
        # v7.1d: surface full stderr so root cause is visible.
        return {
            "success": False,
            "output": "",
            "error": f"Gemini CLI exited code {returncode}: {stderr[:2000]}",
        }

    text_parts: List[str] = []
    files_created: List[str] = []
    last_data = None
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # Strategy 1: locate the FINAL top-level JSON object via brace matching.
    # Gemini emits banners ("Loaded cached credentials.\nYOLO ...\nMCP ...")
    # before the JSON, and the JSON is one big multi-line object — not JSONL.
    raw = stdout.strip()
    parsed_obj = None
    first_brace = raw.find('{')
    if first_brace >= 0:
        depth = 0
        end_idx = -1
        in_string = False
        escape = False
        for i in range(first_brace, len(raw)):
            ch = raw[i]
            if escape:
                escape = False
                continue
            if ch == '\\' and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        if end_idx > 0:
            try:
                parsed_obj = json.loads(raw[first_brace:end_idx + 1])
            except json.JSONDecodeError:
                parsed_obj = None

    if isinstance(parsed_obj, dict):
        last_data = parsed_obj
        # Primary response field
        resp = parsed_obj.get("response") or parsed_obj.get("result") or ""
        if resp:
            text_parts.append(str(resp))
        # Token stats from the multi-model "stats.models" map
        try:
            stats = parsed_obj.get("stats") or {}
            models = stats.get("models") or {}
            if isinstance(models, dict) and models:
                # Sum across all models that ran
                p_tok = c_tok = t_tok = 0
                for m_info in models.values():
                    toks = (m_info or {}).get("tokens") or {}
                    p_tok += int(toks.get("prompt", 0) or 0)
                    c_tok += int(toks.get("candidates", 0) or 0)
                    t_tok += int(toks.get("total", 0) or 0)
                usage = {
                    "prompt_tokens": p_tok,
                    "completion_tokens": c_tok,
                    "total_tokens": t_tok or (p_tok + c_tok),
                }
            # File ops from tool stats
            tools = stats.get("tools") or {}
            by_name = tools.get("byName") or {}
            if isinstance(by_name, dict):
                for tool_name in ("write_file", "edit_file", "create_file", "WriteFile", "Edit"):
                    if tool_name in by_name:
                        # Don't have paths from stats — record marker
                        files_created.append(f"<{tool_name} called>")
        except Exception:
            pass

    # Strategy 2 fallback: line-by-line JSONL (older Gemini versions).
    if not text_parts:
        for line in raw.split('\n'):
            line = line.strip()
            if not line or not line.startswith('{'):
                continue
            try:
                obj = json.loads(line)
                last_data = obj
                if isinstance(obj, dict):
                    for k in ("response", "result", "text", "content", "output"):
                        v = obj.get(k)
                        if v:
                            text_parts.append(str(v))
                            break
            except json.JSONDecodeError:
                continue

    output = "\n".join(t for t in text_parts if t).strip()
    if not output and last_data:
        output = str(last_data)[:2000]

    return {
        "success": returncode == 0 and bool(output),
        "output": output,
        "files_created": files_created,
        "raw_response": last_data,
        "usage": usage,
    }


def _build_kimi_cmd(task: str, workspace: str, timeout: int, **kwargs) -> List[str]:
    """Build Kimi CLI command (Moonshot AI).

    v7.1i (maintainer 2026-04-25): Kimi CLI integration.
    Repo: https://github.com/MoonshotAI/kimi-cli
    Auth: must run `kimi /login` once interactively to save OAuth token,
    THEN subprocess invocations work. There is no KIMI_API_KEY env override
    in v1.39 (as of 2026-04-25).
    Headless: `kimi -p "task" --output-format stream-json -m kimi-k2.6`
    Output: stream-json is JSONL.
    """
    ultra_on = bool(kwargs.get("ultra_mode"))
    # v7.1i (maintainer 2026-04-25): correct kimi syntax — `--print` mode is
    # required for `--output-format` to be valid. Without --print, kimi
    # complains "Output format is only supported for print UI".
    cmd = [
        "kimi",
        "-p", task,
        "--print",
        "--output-format", "stream-json",
        "--yolo",  # v7.1i: auto-approve all tool calls (= claude's --skip-permissions)
    ]
    # v7.1i (maintainer 2026-04-25): kimi --work-dir + --add-dir for workspace
    # awareness (was missing — kimi was reading from CWD only, no scope hint).
    if workspace:
        cmd.extend(["--work-dir", workspace, "--add-dir", workspace])
    model = kwargs.get("model", "")
    if not model and ultra_on:
        model = "kimi-k2.6"
    if model:
        cmd.extend(["-m", model])
    return cmd


def _parse_kimi_output(stdout: str, stderr: str, returncode: int) -> Dict:
    """Parse Kimi CLI stream-json (JSONL) output."""
    if returncode != 0 and not stdout.strip():
        return {
            "success": False, "output": "",
            "error": f"Kimi CLI exited code {returncode}: {stderr[:1000]}",
        }
    text_parts: List[str] = []
    files_created: List[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    last_event = None
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            last_event = obj
            if not isinstance(obj, dict):
                continue
            evt = obj.get("type", "")
            if evt in ("result", "message", "assistant_message", "text"):
                t = obj.get("text") or obj.get("content") or ""
                if isinstance(t, str) and t:
                    text_parts.append(t)
            elif evt in ("tool_call", "tool_use"):
                name = (obj.get("name") or obj.get("tool") or "").lower()
                if name in ("write", "edit", "create_file", "write_file", "str_replace_editor"):
                    args = obj.get("args") or obj.get("arguments") or {}
                    p = (args.get("file_path") or args.get("path") or "").strip()
                    if p:
                        files_created.append(p)
            elif evt == "usage":
                usage = {
                    "prompt_tokens": int(obj.get("input_tokens", 0) or 0),
                    "completion_tokens": int(obj.get("output_tokens", 0) or 0),
                    "total_tokens": int(
                        obj.get("total_tokens", 0)
                        or (int(obj.get("input_tokens", 0) or 0) + int(obj.get("output_tokens", 0) or 0))
                    ),
                }
        except json.JSONDecodeError:
            if line and not line.startswith("{"):
                text_parts.append(line)
    output = "\n".join(t for t in text_parts if t).strip()
    return {
        "success": returncode == 0 and bool(output),
        "output": output,
        "files_created": files_created,
        "raw_response": last_event,
        "usage": usage,
    }


def _build_qwen_cmd(task: str, workspace: str, timeout: int, **kwargs) -> List[str]:
    """Build Qwen Code CLI command. Inherits Gemini CLI flags (it's a fork).

    v7.1i: Qwen Code is a Gemini CLI fork — same -p / --approval-mode yolo
    / -o json / --include-directories interface. Auth via DASHSCOPE_API_KEY
    (Alibaba) or OPENAI_API_KEY (cross-provider).
    """
    ultra_on = bool(kwargs.get("ultra_mode"))
    # .geminiignore precondition (Qwen inherits this)
    try:
        for _dir in ({os.getcwd(), workspace} - {""}):
            if _dir and os.path.isdir(_dir):
                _ig = os.path.join(_dir, ".geminiignore")
                if not os.path.exists(_ig):
                    try:
                        with open(_ig, "w", encoding="utf-8"):
                            pass
                    except Exception:
                        pass
    except Exception:
        pass
    cmd = [
        "qwen",
        "-p", task,
        "--approval-mode", "yolo",
        "-o", "json",
    ]
    model = kwargs.get("model", "")
    if not model and ultra_on:
        model = "qwen3-coder-plus"
    if model:
        cmd.extend(["-m", model])
    if workspace:
        cmd.extend(["--include-directories", workspace])
    return cmd


def _parse_qwen_output(stdout: str, stderr: str, returncode: int) -> Dict:
    """Qwen Code is a Gemini fork — output schema identical, reuse parser."""
    return _parse_gemini_output(stdout, stderr, returncode)


def _build_aider_cmd(task: str, workspace: str, timeout: int, **kwargs) -> List[str]:
    """Build Aider CLI command.
    Ref: aider --message "task" --yes --no-git --model <model>
    """
    cmd = [
        "aider",
        "--message", task,
        "--yes",
        "--no-git",
    ]
    model = kwargs.get("model", "")
    if model:
        cmd.extend(["--model", model])
    return cmd


def _parse_aider_output(stdout: str, stderr: str, returncode: int) -> Dict:
    """Parse Aider output."""
    if returncode != 0 and not stdout.strip():
        return {
            "success": False,
            "output": "",
            "error": f"Aider exited with code {returncode}: {stderr[:500]}",
        }
    # Aider outputs to stdout with ANSI codes — strip them
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean = ansi_escape.sub('', stdout)
    # Try to extract file changes from aider's output
    files_created = []
    for line in clean.split('\n'):
        if line.strip().startswith('Wrote ') or line.strip().startswith('Created '):
            parts = line.strip().split(' ', 1)
            if len(parts) > 1:
                files_created.append(parts[1].strip())
    return {
        "success": returncode == 0,
        "output": clean.strip(),
        "files_created": files_created,
    }


# ── Master CLI registry ──
CLI_PROFILES: Dict[str, CLIProfile] = {
    "claude": CLIProfile(
        name="claude",
        display_name="Claude Code",
        binary_names=["claude"],
        version_cmd=["claude", "--version"],
        build_cmd=_build_claude_cmd,
        parse_output=_parse_claude_output,
        supports_json=True,
        supports_streaming=True,
        supports_file_ops=True,
        max_timeout=600,
    ),
    "codex": CLIProfile(
        name="codex",
        display_name="Codex CLI",
        binary_names=["codex"],
        version_cmd=["codex", "--version"],
        build_cmd=_build_codex_cmd,
        parse_output=_parse_codex_output,
        supports_json=True,
        supports_streaming=False,
        supports_file_ops=True,
        max_timeout=600,
    ),
    "gemini": CLIProfile(
        name="gemini",
        display_name="Gemini CLI",
        binary_names=["gemini"],
        version_cmd=["gemini", "--version"],
        build_cmd=_build_gemini_cmd,
        parse_output=_parse_gemini_output,
        supports_json=True,
        supports_streaming=True,
        supports_file_ops=True,
        max_timeout=300,
    ),
    "aider": CLIProfile(
        name="aider",
        display_name="Aider",
        binary_names=["aider"],
        version_cmd=["aider", "--version"],
        build_cmd=_build_aider_cmd,
        parse_output=_parse_aider_output,
        supports_json=False,
        supports_streaming=False,
        supports_file_ops=True,
        max_timeout=600,
    ),
    "kimi": CLIProfile(
        name="kimi",
        display_name="Kimi CLI (Moonshot)",
        binary_names=["kimi"],
        version_cmd=["kimi", "--version"],
        build_cmd=_build_kimi_cmd,
        parse_output=_parse_kimi_output,
        supports_json=True,
        supports_streaming=True,
        supports_file_ops=True,
        max_timeout=600,
    ),
    "qwen": CLIProfile(
        name="qwen",
        display_name="Qwen Code (Alibaba)",
        binary_names=["qwen"],
        version_cmd=["qwen", "--version"],
        build_cmd=_build_qwen_cmd,
        parse_output=_parse_qwen_output,
        supports_json=True,
        supports_streaming=True,
        supports_file_ops=True,
        max_timeout=600,
    ),
}

# ── Per-CLI model options ──
# Each entry: {id: display_name}
# "id" is what gets passed to --model; "display_name" is shown in the UI.
# First entry is the default model for that CLI.
CLI_MODEL_OPTIONS: Dict[str, List[Dict[str, str]]] = {
    "claude": [
        {"id": "", "name": "Default (account default)"},
        # Claude 4 family
        {"id": "opus", "name": "Claude Opus 4.6 (latest)"},
        {"id": "sonnet", "name": "Claude Sonnet 4.6 (latest)"},
        {"id": "haiku", "name": "Claude Haiku 4.5"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-5-20250514", "name": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
        # Claude 3.5 family
        {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet"},
        {"id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku"},
    ],
    "codex": [
        {"id": "", "name": "Default (account default)"},
        # GPT-5 family
        {"id": "gpt-5", "name": "GPT-5"},
        {"id": "gpt-5.4", "name": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini"},
        {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
        {"id": "gpt-5.2-codex", "name": "GPT-5.2 Codex"},
        # o series
        {"id": "o3", "name": "o3"},
        {"id": "o4-mini", "name": "o4-mini"},
        # GPT-4 family
        {"id": "gpt-4.1", "name": "GPT-4.1"},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini"},
        {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano"},
        {"id": "codex-mini", "name": "Codex Mini"},
    ],
    "gemini": [
        {"id": "", "name": "Default (account default)"},
        # Gemini 3 family
        {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3.0-flash", "name": "Gemini 3.0 Flash"},
        # Gemini 2.5 family
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        # Gemini 2.0 family
        {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
        {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash Lite"},
    ],
    "aider": [
        {"id": "", "name": "Default"},
        # Cross-provider models — Aider supports any LiteLLM-compatible model id.
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "gpt-5.4", "name": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini"},
        {"id": "gpt-4.1", "name": "GPT-4.1"},
        {"id": "gemini/gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        {"id": "deepseek-chat", "name": "DeepSeek Chat"},
    ],
    "kimi": [
        {"id": "", "name": "Default (account default)"},
        {"id": "kimi-k2.6", "name": "Kimi K2.6 (latest, coding)"},
        {"id": "kimi-k2.5", "name": "Kimi K2.5"},
        {"id": "kimi-k2-0905-preview", "name": "Kimi K2 0905 Preview"},
        {"id": "moonshot-v1-128k", "name": "Moonshot v1 128k (general)"},
    ],
    "qwen": [
        {"id": "", "name": "Default (account default)"},
        {"id": "qwen3-coder-plus", "name": "Qwen3 Coder Plus"},
        {"id": "qwen3.6-plus", "name": "Qwen3.6 Plus"},
        {"id": "qwen3.5-plus", "name": "Qwen3.5 Plus"},
        {"id": "qwen-max", "name": "Qwen Max"},
        {"id": "qwen3:32b", "name": "Qwen3 32B (local Ollama)"},
    ],
}


def get_cli_models(cli_name: str) -> List[Dict[str, str]]:
    """Return available models for a given CLI."""
    return CLI_MODEL_OPTIONS.get(cli_name, [{"id": "", "name": "Default"}])


# Node type → preferred CLI order (builder nodes prefer file-capable CLIs)
NODE_CLI_PREFERENCE: Dict[str, List[str]] = {
    # v7.1i (maintainer 2026-04-25): Kimi + Qwen added.
    # Kimi (Moonshot K2.6) is best at Chinese-language coding tasks.
    # Qwen (Alibaba) is Gemini-fork with DashScope API; good Chinese fallback.
    "builder":  ["claude", "codex", "kimi", "qwen", "aider", "gemini"],
    "debugger": ["claude", "codex", "kimi", "aider", "gemini"],
    "merger":   ["claude", "kimi", "codex", "qwen", "gemini"],
    "reviewer": ["claude", "kimi", "qwen", "gemini", "codex"],
    "tester":   ["claude", "kimi", "codex", "qwen", "gemini"],
    "analyst":  ["kimi", "gemini", "qwen", "claude", "codex"],
    "planner":  ["claude", "kimi", "qwen", "gemini", "codex"],
    "_default": ["claude", "kimi", "qwen", "codex", "gemini", "aider"],
}


# ─────────────────────────────────────────────
# CLI Detector — finds installed CLIs on the system
# ─────────────────────────────────────────────

class CLIDetector:
    """Detects and tests available CLI tools on the host machine."""

    def __init__(self):
        self._cache: Dict[str, Dict] = {}
        self._cache_ts: float = 0
        self._cache_ttl: float = 1800  # V4.6 SPEED: 30min cache (was 5min) — CLI binaries don't change often

    def _cache_valid(self) -> bool:
        return bool(self._cache) and (time.time() - self._cache_ts < self._cache_ttl)

    async def detect_all(self, force: bool = False) -> Dict[str, Dict]:
        """Detect all registered CLI tools + scan PATH for extra AI CLIs.
        Returns {name: {available, path, version, ...}}."""
        if not force and self._cache_valid():
            return self._cache

        results = {}
        tasks = []
        for name, profile in CLI_PROFILES.items():
            tasks.append(self._detect_one(name, profile))

        detected = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(CLI_PROFILES.keys(), detected):
            if isinstance(result, Exception):
                results[name] = {
                    "available": False,
                    "name": name,
                    "display_name": CLI_PROFILES[name].display_name,
                    "error": str(result)[:200],
                }
            else:
                results[name] = result

        # ── PATH scan: discover additional AI CLIs not in the registry ──
        extra_scan = await self._scan_path_for_extra_clis(set(results.keys()))
        results.update(extra_scan)

        self._cache = results
        self._cache_ts = time.time()
        return results

    async def _scan_path_for_extra_clis(self, known: set) -> Dict[str, Dict]:
        """Scan PATH for AI-related CLI tools not already in CLI_PROFILES."""
        # Well-known AI CLI binary names to look for
        extra_candidates = {
            "cursor": "Cursor",
            "copilot": "GitHub Copilot CLI",
            "gh-copilot": "GitHub Copilot CLI",
            "cody": "Sourcegraph Cody",
            "continue": "Continue.dev",
            "tabby": "Tabby",
            "ollama": "Ollama",
            "llamafile": "Llamafile",
            "jan": "Jan",
            "lmstudio": "LM Studio CLI",
            "sgpt": "Shell GPT",
            "fabric": "Fabric AI",
            "goose": "Goose AI",
            "amp": "Amp (Sourcegraph)",
            "avante": "Avante",
            "cline": "Cline",
            "roo": "Roo Code",
            "windsurf": "Windsurf",
            "trae": "Trae",
        }
        found = {}
        for bin_name, display_name in extra_candidates.items():
            if bin_name in known:
                continue
            binary_path = shutil.which(bin_name)
            if not binary_path:
                continue
            # Try to get version
            version = ""
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary_path, "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
                raw = stdout.decode("utf-8", errors="replace").strip()
                if not raw:
                    raw = stderr.decode("utf-8", errors="replace").strip()
                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                version = ansi_escape.sub('', raw).split('\n')[0].strip()[:100]
            except Exception:
                pass
            found[bin_name] = {
                "available": True,
                "name": bin_name,
                "display_name": display_name,
                "path": binary_path,
                "version": version,
                "supports_json": False,
                "supports_file_ops": False,
                "supports_streaming": False,
                "is_extra": True,  # Not in the core registry — generic subprocess only
            }
        return found

    async def _detect_one(self, name: str, profile: CLIProfile) -> Dict:
        """Detect a single CLI tool."""
        result = {
            "available": False,
            "name": name,
            "display_name": profile.display_name,
            "path": None,
            "version": None,
            "supports_json": profile.supports_json,
            "supports_file_ops": profile.supports_file_ops,
            "supports_streaming": profile.supports_streaming,
        }

        # Find the binary
        binary_path = None
        for bin_name in profile.binary_names:
            found = shutil.which(bin_name)
            if found:
                binary_path = found
                break

        if not binary_path:
            return result

        result["path"] = binary_path
        profile.detected_path = binary_path

        # Get version
        try:
            proc = await asyncio.create_subprocess_exec(
                *profile.version_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            version_text = stdout.decode("utf-8", errors="replace").strip()
            if not version_text:
                version_text = stderr.decode("utf-8", errors="replace").strip()
            # Extract version number (first line, strip ANSI)
            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            version_text = ansi_escape.sub('', version_text).split('\n')[0].strip()
            result["version"] = version_text[:100]
            profile.detected_version = version_text[:100]
            result["available"] = True
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
            result["error"] = str(e)[:200]

        return result

    async def test_cli(self, cli_name: str) -> Dict:
        """Run a quick smoke test on a specific CLI to verify it works."""
        profile = CLI_PROFILES.get(cli_name)
        if not profile:
            return {"success": False, "error": f"Unknown CLI: {cli_name}"}

        detect_result = await self._detect_one(cli_name, profile)
        if not detect_result.get("available"):
            return {
                "success": False,
                "error": f"{profile.display_name} not found on this system",
                **detect_result,
            }

        # Run a trivial task to verify the CLI is functional
        test_task = 'Reply with exactly: {"status":"ok"}'
        try:
            cmd = profile.build_cmd(test_task, workspace="", timeout=30)
            # Resolve binary to absolute path
            resolved_bin = detect_result.get("path") or cmd[0]
            cmd[0] = resolved_bin

            start = time.time()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1"},
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=60
            )
            elapsed = round(time.time() - start, 2)
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            parsed = profile.parse_output(stdout_text, stderr_text, proc.returncode)
            return {
                "success": parsed.get("success", False),
                "latency_s": elapsed,
                "output_preview": str(parsed.get("output", ""))[:200],
                **detect_result,
            }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"{profile.display_name} test timed out (60s)",
                **detect_result,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"{profile.display_name} test error: {str(e)[:200]}",
                **detect_result,
            }

    def get_available_clis(self) -> List[str]:
        """Return names of CLIs that were detected as available (from cache)."""
        if not self._cache:
            return []
        return [name for name, info in self._cache.items() if info.get("available")]


# ─────────────────────────────────────────────
# CLI Executor — runs tasks through local CLI tools
# ─────────────────────────────────────────────

class CLIExecutor:
    """Executes AI tasks through local CLI subprocess calls.

    Inspired by MCO's ShimTransport pattern: spawn a subprocess, feed it a task,
    collect structured output, normalize into standard result dict.
    """

    def __init__(self, detector: CLIDetector, config: Optional[Dict] = None):
        self.detector = detector
        self.config = config or {}
        # v7.1g (maintainer 2026-04-25): per-CLI fast-fail tracker. When a CLI
        # exits in <10s with no output 3 times in a row, mark it
        # circuit-broken and skip it from rotation for the next 5 minutes.
        # Prevents strict-mode cascade: Gemini quota-exhausted → patcher
        # tries Claude→Codex→Gemini → all fail → strict_failure → run dies.
        self._circuit_state: Dict[str, Dict[str, Any]] = {}

    def _circuit_open(self, cli_name: str) -> bool:
        """Returns True if the CLI should be skipped due to repeated fast failures."""
        st = self._circuit_state.get(cli_name)
        if not st:
            return False
        if st.get("trip_until", 0) > time.time():
            return True
        return False

    def _circuit_record_failure(self, cli_name: str, elapsed: float, output_len: int) -> None:
        st = self._circuit_state.setdefault(
            cli_name, {"fast_fail_streak": 0, "trip_until": 0.0}
        )
        if elapsed < 10 and output_len == 0:
            st["fast_fail_streak"] = int(st.get("fast_fail_streak", 0)) + 1
        else:
            st["fast_fail_streak"] = 0
        if st["fast_fail_streak"] >= 3:
            # Trip: skip for 5 minutes
            st["trip_until"] = time.time() + 300.0
            logger.warning(
                "CLI circuit breaker TRIPPED for %s — skipping for 5 minutes "
                "(3 consecutive fast-fail-no-output)", cli_name,
            )

    def _circuit_record_success(self, cli_name: str) -> None:
        st = self._circuit_state.setdefault(
            cli_name, {"fast_fail_streak": 0, "trip_until": 0.0}
        )
        st["fast_fail_streak"] = 0
        st["trip_until"] = 0.0

    def select_cli(self, node_type: str, available_clis: List[str],
                   preferred_cli: Optional[str] = None) -> Optional[str]:
        """Select the best CLI for a given node type.
        Priority: user preference > node-type preference > first available.
        """
        if preferred_cli and preferred_cli in available_clis:
            return preferred_cli

        preference_order = NODE_CLI_PREFERENCE.get(
            node_type,
            NODE_CLI_PREFERENCE["_default"]
        )
        for cli_name in preference_order:
            if cli_name in available_clis:
                return cli_name

        return available_clis[0] if available_clis else None

    async def execute(
        self,
        task: str,
        node_type: str,
        workspace: str = "",
        timeout: int = 600,
        preferred_cli: Optional[str] = None,
        preferred_model: Optional[str] = None,
        on_progress: Optional[Callable] = None,
        node: Optional[Dict] = None,
        ultra_mode: bool = False,  # v7.1c — passed through to build_cmd for max-turns
    ) -> Dict[str, Any]:
        """Execute a task through a CLI tool with automatic fallback.

        Args:
            preferred_cli: Which CLI to use (claude, codex, gemini, etc.)
            preferred_model: Which model to use within that CLI (e.g., "sonnet", "o3")
        If the preferred CLI fails, tries the next available CLI for this node type.
        Returns a standard result dict compatible with AIBridge.execute() output.
        """
        available = self.detector.get_available_clis()
        if not available:
            await self.detector.detect_all()
            available = self.detector.get_available_clis()
            if not available:
                return {
                    "success": False, "output": "", "mode": "cli_unavailable",
                    "error": "No CLI tools detected. Install Claude Code, Codex, or Gemini CLI.",
                }

        # Build ordered CLI list: preferred first, then node-type preference, then remaining
        cli_order = []
        if preferred_cli and preferred_cli in available:
            cli_order.append(preferred_cli)
        pref_list = NODE_CLI_PREFERENCE.get(node_type, NODE_CLI_PREFERENCE["_default"])
        for c in pref_list:
            if c in available and c not in cli_order:
                cli_order.append(c)
        for c in available:
            if c not in cli_order and c in CLI_PROFILES:
                cli_order.append(c)

        if not cli_order:
            return {
                "success": False, "output": "", "mode": "cli_unavailable",
                "error": f"No suitable CLI found for node type '{node_type}'",
            }

        # v7.1g (maintainer 2026-04-25): circuit-breaker filter.
        # Skip any CLI that's currently tripped due to repeated fast failures.
        # If ALL preferred CLIs are tripped, fall through and try them anyway
        # (better to attempt than to deadlock).
        _untripped = [c for c in cli_order if not self._circuit_open(c)]
        if _untripped:
            cli_order = _untripped
        else:
            logger.warning(
                "All CLIs in rotation are circuit-broken; trying anyway: %s", cli_order,
            )

        last_error = ""
        for attempt_idx, cli_name in enumerate(cli_order):
            profile = CLI_PROFILES.get(cli_name)
            if not profile:
                continue
            detect_info = self.detector._cache.get(cli_name, {})
            binary_path = detect_info.get("path") or profile.detected_path or cli_name

            # Only pass model for the preferred CLI; fallback CLIs use their defaults
            model_for_this_cli = preferred_model if (cli_name == preferred_cli and preferred_model) else ""

            if on_progress:
                fallback_note = f" (fallback #{attempt_idx})" if attempt_idx > 0 else ""
                model_note = f" [{model_for_this_cli}]" if model_for_this_cli else ""
                try:
                    await on_progress({
                        "stage": "cli_dispatch",
                        "message": f"[终端]️ Using {profile.display_name}{model_note} CLI{fallback_note}",
                        "cli_name": cli_name,
                        "cli_path": binary_path,
                        "model": model_for_this_cli,
                    })
                except Exception:
                    pass

            # v7.1g (maintainer 2026-04-25): session-seed derivation.
            # Original idea: stable seed from (run_id + node_key) so retries
            # share prompt cache. PROBLEM: UUID5 of stable seed is
            # deterministic → if the orchestrator re-dispatches the same
            # node (or two concurrent attempts run), Claude returns
            # "Session ID X is already in use" and exits 1 within 0.3s.
            # FIX: include node_execution_id (unique per dispatch) AND a
            # millisecond timestamp to break collisions, while keeping
            # SAME node_execution within a single retry sharing cache via
            # the --append-system-prompt path (which caches the system
            # block independently of session-id).
            _session_seed = ""
            try:
                _ne_id = str((node or {}).get("node_execution_id", "")).strip().lower()
                _run_id_for_seed = str((node or {}).get("run_id", "")).strip().lower()
                _node_key_for_seed = (
                    str((node or {}).get("key", "")).strip().lower()
                    or str((node or {}).get("node_key", "")).strip().lower()
                    or node_type
                )
                # v7.1i (maintainer 2026-04-25 — 2nd revision):
                # Stable seed for cache reuse, BUT add a "process boot"
                # discriminator so cross-process / cross-restart never
                # collides with stale session jsonl files left by previous
                # runs in ~/.claude/projects/. Claude exits 1 with "Session
                # ID X is already in use" when it sees a stale jsonl on
                # disk — we observed this in run_0811100214c0 router lane
                # because router has empty run_id (it's a pre-NE call) so
                # `evermind::router` produced the SAME UUID5 across runs.
                # Discriminator = boot timestamp of this server process
                # (constant within process, unique across restarts).
                _BOOT_DISCRIMINATOR = str(int(globals().get("_PROC_BOOT_TS", 0)))
                if not _BOOT_DISCRIMINATOR or _BOOT_DISCRIMINATOR == "0":
                    import time as _bt
                    globals()["_PROC_BOOT_TS"] = int(_bt.time())
                    _BOOT_DISCRIMINATOR = str(globals()["_PROC_BOOT_TS"])
                _seed_run = _run_id_for_seed or "norun"
                if _ne_id:
                    _session_seed = f"evermind:{_BOOT_DISCRIMINATOR}:{_seed_run}:{_ne_id}"
                elif _node_key_for_seed:
                    _session_seed = f"evermind:{_BOOT_DISCRIMINATOR}:{_seed_run}:{_node_key_for_seed}"
            except Exception:
                pass

            # Pull the CLI-specialization header out so it can be
            # appended via --append-system-prompt for cache friendliness
            # (instead of stuffing the whole 40KB into the user task).
            _append_sys = str(
                (node or {}).get("_cli_append_system_prompt", "")
                if isinstance(node, dict) else ""
            )

            # v7.1g: Codex output-schema per node-type (kills narration).
            # Maps node_type → schema file in ~/.openclaw/workspace/codex_schemas/
            _output_schema_path = ""
            if cli_name == "codex":
                _CODEX_SCHEMA_MAP = {
                    "builder": "builder",
                    "merger":  "merger",
                    "patcher": "patcher",
                    "polisher": "merger",   # similar shape: lanes/files/summary
                    "debugger": "patcher",
                }
                _schema_name = _CODEX_SCHEMA_MAP.get(node_type, "")
                if _schema_name:
                    _candidate = (
                        Path.home() / ".openclaw" / "workspace"
                        / "codex_schemas" / f"{_schema_name}.json"
                    )
                    if _candidate.exists():
                        _output_schema_path = str(_candidate)

            result = await self._run_single_cli(
                cli_name, profile, binary_path, task, node_type,
                workspace, timeout, on_progress, node,
                model=model_for_this_cli,
                ultra_mode=ultra_mode,
                session_seed=_session_seed,
                append_system_prompt=_append_sys,
                output_schema_path=_output_schema_path,
            )
            # v7.1g circuit breaker bookkeeping
            if result.get("success"):
                self._circuit_record_success(cli_name)
                return result
            self._circuit_record_failure(
                cli_name,
                elapsed=float(result.get("cli_latency_s", 99) or 99),
                output_len=len(str(result.get("output") or "")),
            )

            last_error = result.get("error", "unknown error")
            logger.warning(
                "CLI fallback: cli=%s failed (%s), trying next...",
                cli_name, last_error[:100],
            )

        # All CLIs failed
        return {
            "success": False, "output": "", "mode": "cli:all_failed",
            "error": f"All CLIs failed. Last error: {last_error}",
            "cli_used": cli_order[0] if cli_order else "",
        }

    async def _run_single_cli(
        self,
        cli_name: str,
        profile: CLIProfile,
        binary_path: str,
        task: str,
        node_type: str,
        workspace: str,
        timeout: int,
        on_progress: Optional[Callable],
        node: Optional[Dict],
        model: str = "",
        ultra_mode: bool = False,
        session_seed: str = "",
        append_system_prompt: str = "",
        output_schema_path: str = "",
    ) -> Dict[str, Any]:
        """Run a single CLI attempt. Returns result dict."""
        full_task = self._build_task_prompt(task, node_type, node, workspace)
        cmd = profile.build_cmd(
            full_task,
            workspace=workspace, timeout=timeout, model=model,
            ultra_mode=ultra_mode,
            session_seed=session_seed,
            append_system_prompt=append_system_prompt,
            node_type=node_type,
            output_schema_path=output_schema_path,
        )
        cmd[0] = binary_path

        logger.info(
            "CLI execute: cli=%s model=%s node=%s timeout=%ds cmd_len=%d",
            cli_name, model or "(default)", node_type, timeout, len(cmd),
        )
        # v7.1g (maintainer 2026-04-25): per-Gemini-failure debug. Log cmd + cwd
        # the first time we see Gemini exit fast so we can replay manually.
        if cli_name == "gemini":
            try:
                _gem_cwd = workspace if workspace and os.path.isdir(workspace) else os.getcwd()
                _task_len = len(cmd[2]) if len(cmd) > 2 else 0
                logger.info(
                    "GEMINI DEBUG: cwd=%s task_len=%d argv[3:]=%s",
                    _gem_cwd, _task_len, cmd[3:],
                )
            except Exception:
                pass

        start_time = time.time()
        # v7.0c (maintainer 2026-04-24): codex temp-auth for GMN relay.
        # codex reads ~/.codex/auth.json (not env vars) for OPENAI_API_KEY.
        # Mirror maintainer's zshrc __codex_with_temp_auth: back up existing
        # auth.json, write the GMN key, run command, restore.
        _codex_auth_backup: Optional[Path] = None
        _codex_auth_file = Path.home() / ".codex" / "auth.json"
        _gmn_key = os.getenv("GMN_OPENAI_API_KEY", "").strip()
        if cli_name == "codex" and _gmn_key:
            try:
                _codex_auth_file.parent.mkdir(parents=True, exist_ok=True)
                if _codex_auth_file.exists():
                    _codex_auth_backup = Path(tempfile.mkstemp(
                        prefix="codex-auth-", suffix=".json"
                    )[1])
                    _codex_auth_backup.write_bytes(_codex_auth_file.read_bytes())
                _codex_auth_file.write_text(
                    json.dumps({"OPENAI_API_KEY": _gmn_key}),
                    encoding="utf-8",
                )
            except Exception as _auth_err:
                logger.warning(
                    "codex GMN temp-auth setup failed: %s — continuing with existing auth",
                    str(_auth_err)[:150],
                )
                _codex_auth_backup = None

        try:
            # v7.1g (maintainer 2026-04-24): subprocess hardening across all CLIs.
            # 1. NO_COLOR/TERM=dumb already in place.
            # 2. NO_BROWSER=true: prevent Gemini OAuth from trying to spawn
            #    a browser (subprocess has no TTY → exit 1 within 3-4s).
            # 3. CI=1: most CLIs treat this as "non-interactive" hint.
            # 4. stdin=DEVNULL (NOT PIPE): prevents Gemini's readline from
            #    hanging waiting for stdin EOF — observed root cause of
            #    the silent 3-second exit-1 flake in run_b318e114b689.
            env = {
                **os.environ,
                # v7.1i (maintainer 2026-04-25): TERM=dumb let gemini 0.39 print
                # "Basic terminal detected — stability and security degraded"
                # warning every call (annoyance + reduces gemini ANSI tools).
                # NO_COLOR=1 already prevents color output across all CLIs,
                # so we can use a real terminal name without losing the
                # plain-text guarantee. Tested with claude/gemini/codex/kimi
                # all happy on xterm-256color + NO_COLOR=1.
                "NO_COLOR": "1", "TERM": "xterm-256color",
                "CI": "1",
                "NO_BROWSER": "true",
                "GEMINI_NO_BROWSER": "1",
                # v7.1i: trust workspace for gemini headless (replaces
                # `--skip-trust` flag which was 'unsupported' since 0.39).
                "GEMINI_CLI_TRUST_WORKSPACE": "true",
            }
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace if workspace and os.path.isdir(workspace) else None,
                env=env,
            )

            if on_progress:
                try:
                    await on_progress({
                        "stage": "cli_running",
                        "message": f"[等待] {profile.display_name} working...",
                        "cli_name": cli_name,
                    })
                except Exception:
                    pass

            # v7.1i (maintainer 2026-04-25): stdout-idle watchdog for gemini.
            # gemini-3.1-pro-preview has a known deadlock pattern (issue
            # #21937 / #22415 / #25192 / #21143): server-side stream never
            # closes, CLI waits forever at "Thinking", 0% CPU, no output.
            # Without watchdog the process eats `timeout` (3600s default).
            # With watchdog: if no stdout byte for IDLE_TIMEOUT seconds, kill
            # — cli_executor falls back to next CLI / retries.
            # Only applied to gemini (claude/codex have their own internal
            # protections). Read stdout incrementally to track idle time.
            if cli_name == "gemini":
                IDLE_TIMEOUT_S = 90      # No new stdout byte for 90s = deadlock
                CHECK_INTERVAL_S = 5
                _stdout_chunks: List[bytes] = []
                _stderr_chunks: List[bytes] = []
                _last_stdout_at = time.time()
                _hard_deadline = start_time + timeout
                _killed_for_idle = False

                async def _drain_stream(stream, chunks: List[bytes], on_byte):
                    while True:
                        chunk = await stream.read(4096)
                        if not chunk:
                            return
                        chunks.append(chunk)
                        on_byte()

                _drain_stdout_task = asyncio.create_task(_drain_stream(
                    proc.stdout, _stdout_chunks,
                    lambda: globals().__setitem__("_LAST_STDOUT_HOLDER", time.time()) or _stdout_chunks
                ))
                # Simpler: track idle in this scope
                async def _watch_stdout(stream, chunks):
                    nonlocal _last_stdout_at
                    while True:
                        chunk = await stream.read(4096)
                        if not chunk:
                            return
                        chunks.append(chunk)
                        _last_stdout_at = time.time()
                _drain_stdout_task.cancel()
                try: await _drain_stdout_task
                except: pass
                _drain_stdout_task = asyncio.create_task(_watch_stdout(proc.stdout, _stdout_chunks))
                _drain_stderr_task = asyncio.create_task(_watch_stdout(proc.stderr, _stderr_chunks))

                while not _drain_stdout_task.done() or not _drain_stderr_task.done():
                    now = time.time()
                    if now > _hard_deadline:
                        logger.warning(
                            "[gemini watchdog] hard timeout %ds reached for node=%s, killing",
                            timeout, node_type,
                        )
                        try: proc.kill()
                        except Exception: pass
                        _killed_for_idle = True
                        break
                    idle_for = now - _last_stdout_at
                    if idle_for > IDLE_TIMEOUT_S:
                        logger.warning(
                            "[gemini watchdog] stdout-idle %.0fs > %ds for node=%s — "
                            "DEADLOCK detected (gemini-3.1-pro-preview issue #21937/#22415). Killing.",
                            idle_for, IDLE_TIMEOUT_S, node_type,
                        )
                        try: proc.kill()
                        except Exception: pass
                        _killed_for_idle = True
                        break
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(_drain_stdout_task, _drain_stderr_task,
                                           return_exceptions=True),
                            timeout=CHECK_INTERVAL_S,
                        )
                    except asyncio.TimeoutError:
                        continue

                # Wait for proc to terminate after kill (or natural exit)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    try: proc.terminate()
                    except Exception: pass
                    try: await asyncio.wait_for(proc.wait(), timeout=5)
                    except: pass
                stdout_bytes = b"".join(_stdout_chunks)
                stderr_bytes = b"".join(_stderr_chunks)
                if _killed_for_idle:
                    stderr_bytes += b"\n[evermind watchdog] killed for stdout-idle deadlock"
            else:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            elapsed = round(time.time() - start_time, 2)

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            if stderr_text.strip():
                logger.debug("CLI stderr (%s): %s", cli_name, stderr_text[:500])

            # v7.1g (maintainer 2026-04-25): persist full Gemini stderr to /tmp
            # for any failure that exits in <10s with no stdout. Lets us
            # diagnose the orchestrator-only 3-4s exit-1 mystery.
            # v7.1i: ALSO log gemini stderr to backend log for any short fast-fail
            # (was only writing to /tmp dump, hard to spot in production tail).
            if cli_name == "gemini" and proc.returncode != 0 and elapsed < 30 and not stdout_text.strip():
                logger.warning(
                    "[gemini fast-fail] node=%s elapsed=%.1fs rc=%s stderr_head=%s",
                    node_type, elapsed, proc.returncode, stderr_text[:600].replace("\n", " | ")
                )
                try:
                    _gem_dump = Path("/tmp") / f"gemini_failure_{int(time.time())}_{node_type}.log"
                    _gem_dump.write_text(
                        f"=== CMD ===\n{' '.join(cmd)}\n\n"
                        f"=== CWD ===\n{workspace}\n\n"
                        f"=== EXIT ===\n{proc.returncode} after {elapsed}s\n\n"
                        f"=== STDOUT ===\n{stdout_text}\n\n"
                        f"=== STDERR (full) ===\n{stderr_text}\n",
                        encoding="utf-8",
                    )
                    logger.warning(
                        "GEMINI FAILURE DUMP: %s (cmd_len=%d task_len=%d cwd=%s)",
                        _gem_dump, len(cmd), len(cmd[2]) if len(cmd) > 2 else 0, workspace,
                    )
                except Exception as _dump_err:
                    logger.debug("Failed to dump Gemini stderr: %s", _dump_err)

            parsed = profile.parse_output(stdout_text, stderr_text, proc.returncode)

            # Merge usage/cost from parser (Claude returns these)
            usage = parsed.get("usage", {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            })
            cost = parsed.get("cost", 0)

            result = {
                "success": parsed.get("success", False),
                "output": parsed.get("output", ""),
                "error": parsed.get("error", ""),
                "files_created": parsed.get("files_created", []),
                "tool_results": [],
                "usage": usage,
                "cost": cost,
                "iterations": 1,
                "mode": f"cli:{cli_name}",
                "assigned_model": f"cli:{cli_name}",
                "cli_used": cli_name,
                "cli_display_name": profile.display_name,
                "cli_model": model or "(default)",
                "cli_latency_s": elapsed,
                "cli_returncode": proc.returncode,
            }

            if on_progress:
                status = "[通过]" if result["success"] else "[X]"
                try:
                    await on_progress({
                        "stage": "cli_complete",
                        "message": f"{status} {profile.display_name} finished ({elapsed}s)",
                        "cli_name": cli_name,
                        "success": result["success"],
                        "latency_s": elapsed,
                    })
                except Exception:
                    pass

            logger.info(
                "CLI result: cli=%s node=%s success=%s elapsed=%.1fs output_len=%d cost=$%.4f",
                cli_name, node_type, result["success"], elapsed,
                len(result.get("output", "")), cost,
            )
            return result

        except asyncio.TimeoutError:
            elapsed = round(time.time() - start_time, 2)
            logger.error("CLI timeout: cli=%s node=%s timeout=%ds", cli_name, node_type, timeout)
            try:
                proc.kill()
            except Exception:
                pass
            return {
                "success": False, "output": "",
                "error": f"{profile.display_name} timed out after {timeout}s",
                "mode": f"cli:{cli_name}:timeout",
                "assigned_model": f"cli:{cli_name}",
                "cli_used": cli_name, "cli_latency_s": elapsed,
            }
        except Exception as e:
            elapsed = round(time.time() - start_time, 2)
            logger.error("CLI error: cli=%s node=%s error=%s", cli_name, node_type, str(e)[:200])
            return {
                "success": False, "output": "",
                "error": f"{profile.display_name} error: {str(e)[:300]}",
                "mode": f"cli:{cli_name}:error",
                "assigned_model": f"cli:{cli_name}",
                "cli_used": cli_name, "cli_latency_s": elapsed,
            }
        finally:
            # v7.0c: restore codex auth.json after the call so interactive
            # codex sessions keep working.
            if cli_name == "codex" and _gmn_key:
                try:
                    if _codex_auth_backup and _codex_auth_backup.exists():
                        _codex_auth_file.write_bytes(_codex_auth_backup.read_bytes())
                        _codex_auth_backup.unlink(missing_ok=True)
                    else:
                        if _codex_auth_file.exists():
                            _codex_auth_file.unlink(missing_ok=True)
                except Exception as _restore_err:
                    logger.debug("codex auth restore failed (non-critical): %s", _restore_err)

    def _build_task_prompt(self, task: str, node_type: str,
                           node: Optional[Dict], workspace: str) -> str:
        """Build a complete task prompt with node context for CLI execution."""
        parts = []

        # Role context
        role_map = {
            "builder": "You are a senior software engineer. Write production-quality code.",
            "reviewer": "You are a code reviewer. Analyze the code for bugs, security issues, and best practices.",
            "tester": "You are a QA engineer. Write and run tests.",
            "debugger": "You are a debugging expert. Find and fix the root cause.",
            "analyst": "You are a research analyst. Gather relevant information and provide analysis.",
            "planner": "You are a technical architect. Create a detailed implementation plan.",
            "merger": "You are a code integration expert. Merge multiple code contributions into a unified codebase.",
        }
        role = role_map.get(node_type, "You are a helpful AI assistant.")
        parts.append(role)

        if workspace:
            parts.append(f"Working directory: {workspace}")

        # Append the output directory so CLI tools know where to write files
        output_dir = self.config.get("output_dir", "/tmp/evermind_output")
        parts.append(f"Output directory: {output_dir}")
        parts.append(f"Write all generated files to the output directory.")

        parts.append("")
        parts.append("## Task")
        parts.append(task)

        return "\n".join(parts)


# ─────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────
_detector = CLIDetector()
_executor: Optional[CLIExecutor] = None


def get_detector() -> CLIDetector:
    return _detector


def get_executor(config: Optional[Dict] = None) -> CLIExecutor:
    global _executor
    if _executor is None or config is not None:
        _executor = CLIExecutor(_detector, config)
    return _executor


def is_cli_mode_enabled(settings: Optional[Dict] = None) -> bool:
    """Check if CLI mode is enabled in settings."""
    if settings is None:
        try:
            from settings import load_settings
            settings = load_settings()
        except Exception:
            return False
    cli_mode = settings.get("cli_mode", {})
    if isinstance(cli_mode, dict):
        return bool(cli_mode.get("enabled", False))
    return False
