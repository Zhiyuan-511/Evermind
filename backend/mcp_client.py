"""v5.5 MCP (Model Context Protocol) client — connect Evermind to external
MCP servers so users can plug in community tools (context7, brave-search,
sqlite, slack, github, filesystem, etc.) without writing glue code.

Minimal POC scope:
  - Discover configured MCP servers from user settings
  - Spawn them over stdio (the most common transport)
  - List their available tools
  - Invoke a tool and stream the result

For the full spec see: https://modelcontextprotocol.io/docs
Reference impls we surveyed:
  - anthropics/mcp-python-sdk (official)
  - Cline / Continue / Cursor all use stdio transport with JSON-RPC
  - Claude Desktop uses the same protocol

Config shape (stored in ~/.evermind/config.json):
    {
      "mcp_servers": [
        {
          "name": "context7",
          "command": "npx",
          "args": ["-y", "@upstash/context7-mcp"],
          "env": {}
        },
        {
          "name": "filesystem",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/evermind_output"],
        }
      ]
    }

Integration with Evermind:
  - An orchestrator can call `list_mcp_tools()` to discover available tools.
  - Each MCP tool is exposed to agents as a standard Evermind plugin
    wrapping `call_mcp_tool(server_name, tool_name, arguments)`.
  - Server processes are long-lived (spawned once per backend session)
    and cleaned up via `shutdown_all()` in server.py lifespan.

Current status: STDIO transport only; streamable HTTP (2025 spec) is TODO.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evermind.mcp_client")


class MCPServerHandle:
    """Thin handle around a running MCP server subprocess.
    Uses asyncio subprocess + JSON-RPC 2.0 over stdio.
    Deliberately minimal — no resilience, no reconnect, no SSE."""

    def __init__(self, name: str, command: str, args: List[str], env: Optional[Dict[str, str]] = None) -> None:
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._tools_cache: Optional[List[Dict[str, Any]]] = None
        self._started_at: float = 0.0

    async def start(self) -> bool:
        if self._proc and self._proc.returncode is None:
            return True
        # Resolve command on PATH so "npx" / "python" / "node" work reliably.
        resolved = shutil.which(self.command) or self.command
        merged_env = {**os.environ, **self.env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                resolved, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
            )
        except Exception as exc:
            logger.warning("Failed to spawn MCP server %s: %s", self.name, exc)
            self._proc = None
            return False
        self._started_at = time.time()
        # Initialize handshake
        try:
            await self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "evermind", "version": "5.5"},
                },
                timeout=8.0,
            )
            await self._notify("notifications/initialized", {})
            logger.info("MCP server %s initialized", self.name)
            return True
        except Exception as exc:
            logger.warning("MCP server %s initialize failed: %s", self.name, exc)
            await self.stop()
            return False

    async def stop(self) -> None:
        if not self._proc:
            return
        try:
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        except Exception as exc:
            logger.debug("MCP stop %s: %s", self.name, exc)
        finally:
            self._proc = None
            self._tools_cache = None

    async def _rpc(self, method: str, params: Dict[str, Any], timeout: float = 15.0) -> Any:
        async with self._lock:
            if not self._proc or self._proc.returncode is not None:
                raise RuntimeError(f"MCP server {self.name} is not running")
            req_id = self._next_id
            self._next_id += 1
            payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            assert self._proc.stdin is not None
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
            # Read lines until we get our response (server may interleave notifications)
            deadline = time.time() + timeout
            assert self._proc.stdout is not None
            while time.time() < deadline:
                raw_line = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=max(0.1, deadline - time.time()),
                )
                if not raw_line:
                    raise RuntimeError(f"MCP server {self.name} closed stdout")
                try:
                    msg = json.loads(raw_line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                if not isinstance(msg, dict) or msg.get("id") != req_id:
                    continue
                if "error" in msg:
                    raise RuntimeError(f"MCP {method} error: {msg['error']}")
                return msg.get("result")
            raise asyncio.TimeoutError(f"MCP {method} timeout after {timeout}s")

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        if not self._proc or self._proc.returncode is not None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        assert self._proc.stdin is not None
        self._proc.stdin.write(line)
        try:
            await self._proc.stdin.drain()
        except Exception:
            pass

    async def list_tools(self) -> List[Dict[str, Any]]:
        if self._tools_cache is not None:
            return self._tools_cache
        result = await self._rpc("tools/list", {})
        tools = (result or {}).get("tools") or []
        self._tools_cache = [t for t in tools if isinstance(t, dict)]
        return self._tools_cache

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._rpc(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=30.0,
        )
        return result or {}


class MCPRegistry:
    """Process-wide registry of live MCP server handles."""

    def __init__(self) -> None:
        self._servers: Dict[str, MCPServerHandle] = {}
        self._lock = threading.Lock()

    async def ensure_started(self, config: List[Dict[str, Any]]) -> List[str]:
        """Idempotent: start any configured server not yet running. Returns active names."""
        started: List[str] = []
        for entry in config or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            cmd = str(entry.get("command") or "").strip()
            if not name or not cmd:
                continue
            with self._lock:
                handle = self._servers.get(name)
                if handle is None:
                    handle = MCPServerHandle(
                        name=name,
                        command=cmd,
                        args=list(entry.get("args") or []),
                        env=dict(entry.get("env") or {}),
                    )
                    self._servers[name] = handle
            ok = await handle.start()
            if ok:
                started.append(name)
        return started

    async def shutdown_all(self) -> None:
        with self._lock:
            handles = list(self._servers.values())
            self._servers.clear()
        for h in handles:
            try:
                await h.stop()
            except Exception:
                pass

    async def list_all_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """name -> list of tool descriptors."""
        result: Dict[str, List[Dict[str, Any]]] = {}
        for name, handle in list(self._servers.items()):
            try:
                result[name] = await handle.list_tools()
            except Exception as exc:
                logger.debug("MCP %s list_tools failed: %s", name, exc)
                result[name] = []
        return result

    async def call(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            handle = self._servers.get(server_name)
        if not handle:
            raise KeyError(f"MCP server '{server_name}' not registered")
        return await handle.call_tool(tool_name, arguments)


_REGISTRY: Optional[MCPRegistry] = None


def registry() -> MCPRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = MCPRegistry()
    return _REGISTRY


async def start_configured_servers(config_section: List[Dict[str, Any]]) -> List[str]:
    """Called from server.py lifespan after config load. Silent no-op when empty."""
    if not config_section:
        return []
    return await registry().ensure_started(config_section)


async def shutdown_configured_servers() -> None:
    """Called from server.py lifespan shutdown."""
    await registry().shutdown_all()


__all__ = [
    "MCPServerHandle",
    "MCPRegistry",
    "registry",
    "start_configured_servers",
    "shutdown_configured_servers",
]
