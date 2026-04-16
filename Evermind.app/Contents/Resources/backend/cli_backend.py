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
    Note: Do NOT use --bare, it disables OAuth/keychain auth and only accepts ANTHROPIC_API_KEY.
    Ref: claude -p "task" --output-format json --model sonnet
    """
    cmd = [
        "claude",
        "-p", task,
        "--output-format", "json",
        "--allowedTools", "Read,Edit,Write,Bash,Glob,Grep",
        "--dangerously-skip-permissions",
    ]
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
    Ref: codex exec --json --full-auto -m o3 -C <dir> "task"
    """
    cmd = [
        "codex", "exec",
        "--json",
        "--full-auto",
        "--skip-git-repo-check",
    ]
    model = kwargs.get("model", "")
    if model:
        cmd.extend(["-m", model])
    if workspace:
        cmd.extend(["-C", workspace])
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
    Ref: gemini -p "task" --yolo -o json -m gemini-2.5-pro
    """
    cmd = ["gemini", "-p", task, "--yolo", "-o", "json"]
    model = kwargs.get("model", "")
    if model:
        cmd.extend(["-m", model])
    if workspace:
        cmd.extend(["--include-directories", workspace])
    return cmd


def _parse_gemini_output(stdout: str, stderr: str, returncode: int) -> Dict:
    """Parse Gemini CLI output.

    With -o json, Gemini outputs JSONL (one JSON object per line).
    The final object typically has the agent's response.
    """
    if returncode != 0 and not stdout.strip():
        return {
            "success": False,
            "output": "",
            "error": f"Gemini CLI exited with code {returncode}: {stderr[:500]}",
        }
    # Try parsing JSONL (multiple JSON objects, one per line)
    lines = stdout.strip().split('\n')
    text_parts = []
    files_created = []
    last_data = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            last_data = obj
            if isinstance(obj, dict):
                # Extract text from various Gemini output formats
                if obj.get("type") == "text" or "text" in obj:
                    text_parts.append(obj.get("text", obj.get("content", "")))
                elif obj.get("type") == "result":
                    text_parts.append(obj.get("result", obj.get("response", "")))
                elif obj.get("response"):
                    text_parts.append(obj["response"])
                elif obj.get("output"):
                    text_parts.append(obj["output"])
        except json.JSONDecodeError:
            # Plain text line
            text_parts.append(line)

    output = "\n".join(t for t in text_parts if t)
    if not output and last_data and isinstance(last_data, dict):
        output = str(last_data)

    return {
        "success": returncode == 0 and bool(output),
        "output": output,
        "files_created": files_created,
        "raw_response": last_data,
    }


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
}

# ── Per-CLI model options ──
# Each entry: {id: display_name}
# "id" is what gets passed to --model; "display_name" is shown in the UI.
# First entry is the default model for that CLI.
CLI_MODEL_OPTIONS: Dict[str, List[Dict[str, str]]] = {
    "claude": [
        {"id": "", "name": "Default (account default)"},
        # Claude 4 系列
        {"id": "opus", "name": "Claude Opus 4.6 (latest)"},
        {"id": "sonnet", "name": "Claude Sonnet 4.6 (latest)"},
        {"id": "haiku", "name": "Claude Haiku 4.5"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-5-20250514", "name": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
        # Claude 3.5 系列
        {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet"},
        {"id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku"},
    ],
    "codex": [
        {"id": "", "name": "Default (account default)"},
        # GPT-5 系列
        {"id": "gpt-5", "name": "GPT-5"},
        {"id": "gpt-5.4", "name": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini"},
        {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
        {"id": "gpt-5.2-codex", "name": "GPT-5.2 Codex"},
        # o 系列
        {"id": "o3", "name": "o3"},
        {"id": "o4-mini", "name": "o4-mini"},
        # GPT-4 系列
        {"id": "gpt-4.1", "name": "GPT-4.1"},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini"},
        {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano"},
        {"id": "codex-mini", "name": "Codex Mini"},
    ],
    "gemini": [
        {"id": "", "name": "Default (account default)"},
        # Gemini 3 系列
        {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3.0-flash", "name": "Gemini 3.0 Flash"},
        # Gemini 2.5 系列
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        # Gemini 2.0 系列
        {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
        {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash Lite"},
    ],
    "aider": [
        {"id": "", "name": "Default"},
        # 跨厂商模型 — Aider 支持任意 LiteLLM 模型名
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "gpt-5.4", "name": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini"},
        {"id": "gpt-4.1", "name": "GPT-4.1"},
        {"id": "gemini/gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        {"id": "deepseek-chat", "name": "DeepSeek Chat"},
    ],
}


def get_cli_models(cli_name: str) -> List[Dict[str, str]]:
    """Return available models for a given CLI."""
    return CLI_MODEL_OPTIONS.get(cli_name, [{"id": "", "name": "Default"}])


# Node type → preferred CLI order (builder nodes prefer file-capable CLIs)
NODE_CLI_PREFERENCE: Dict[str, List[str]] = {
    "builder": ["claude", "codex", "aider", "gemini"],
    "debugger": ["claude", "codex", "aider", "gemini"],
    "merger": ["claude", "codex", "gemini"],
    "reviewer": ["claude", "gemini", "codex"],
    "tester": ["claude", "codex", "gemini"],
    "analyst": ["gemini", "claude", "codex"],
    "planner": ["claude", "gemini", "codex"],
    "_default": ["claude", "codex", "gemini", "aider"],
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
                        "message": f"🖥️ Using {profile.display_name}{model_note} CLI{fallback_note}",
                        "cli_name": cli_name,
                        "cli_path": binary_path,
                        "model": model_for_this_cli,
                    })
                except Exception:
                    pass

            result = await self._run_single_cli(
                cli_name, profile, binary_path, task, node_type,
                workspace, timeout, on_progress, node,
                model=model_for_this_cli,
            )
            if result["success"]:
                return result

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
    ) -> Dict[str, Any]:
        """Run a single CLI attempt. Returns result dict."""
        full_task = self._build_task_prompt(task, node_type, node, workspace)
        cmd = profile.build_cmd(full_task, workspace=workspace, timeout=timeout, model=model)
        cmd[0] = binary_path

        logger.info(
            "CLI execute: cli=%s model=%s node=%s timeout=%ds cmd_len=%d",
            cli_name, model or "(default)", node_type, timeout, len(cmd),
        )

        start_time = time.time()
        try:
            env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace if workspace and os.path.isdir(workspace) else None,
                env=env,
            )

            if on_progress:
                try:
                    await on_progress({
                        "stage": "cli_running",
                        "message": f"⏳ {profile.display_name} working...",
                        "cli_name": cli_name,
                    })
                except Exception:
                    pass

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            elapsed = round(time.time() - start_time, 2)

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            if stderr_text.strip():
                logger.debug("CLI stderr (%s): %s", cli_name, stderr_text[:500])

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
                status = "✅" if result["success"] else "❌"
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
