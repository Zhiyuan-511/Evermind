"""v5.5 MCP plugin adapter — exposes each tool from a running MCP server
as an Evermind Plugin so agents can call it like any built-in tool.

Registration happens at backend startup after MCP servers initialize:
    # in server.py lifespan, after start_configured_servers:
    await register_mcp_tools_as_plugins()

The adapter dynamically creates one Plugin subclass per (server, tool) pair
so the OpenAI function-calling tool list can include them alongside
file_ops / browser / shell / etc.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from plugins.base import Plugin, PluginRegistry, PluginResult, SecurityLevel

logger = logging.getLogger("evermind.plugins.mcp")


def _normalize_plugin_name(server_name: str, tool_name: str) -> str:
    """Produce a safe, predictable plugin name: 'mcp__server__tool'.
    OpenAI tool-calling names must match `^[a-zA-Z0-9_-]{1,64}$` so we
    sanitize by replacing any non-conforming chars with underscores."""
    safe_server = "".join(c if c.isalnum() or c in "_-" else "_" for c in server_name)[:24]
    safe_tool = "".join(c if c.isalnum() or c in "_-" else "_" for c in tool_name)[:32]
    name = f"mcp__{safe_server}__{safe_tool}"
    return name[:64]


class MCPToolPlugin(Plugin):
    """Wraps a single MCP server tool as an Evermind plugin."""

    def __init__(
        self,
        *,
        server_name: str,
        tool_name: str,
        description: str,
        parameters_schema: Dict[str, Any],
    ) -> None:
        super().__init__()
        self._server_name = server_name
        self._tool_name = tool_name
        self.name = _normalize_plugin_name(server_name, tool_name)
        self.display_name = f"MCP · {server_name} · {tool_name}"
        self.description = str(description or f"MCP tool `{tool_name}` from server `{server_name}`.")[:600]
        # L2 (moderate) — MCP tools can do file / network / subprocess work.
        # Users who don't want this can avoid installing MCP servers entirely.
        self.security_level = SecurityLevel.L2
        self.icon = "🔌"
        self._parameters_schema = parameters_schema or {"type": "object", "properties": {}, "required": []}

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return self._parameters_schema

    async def execute(self, params: Dict[str, Any], context: Optional[Dict] = None) -> PluginResult:
        try:
            import mcp_client
            raw = await mcp_client.registry().call(self._server_name, self._tool_name, params or {})
        except Exception as exc:
            return PluginResult(success=False, error=f"MCP call failed: {exc}"[:400])
        # MCP tools/call returns {content: [{type, text}], isError?}
        is_error = bool((raw or {}).get("isError"))
        content_blocks = (raw or {}).get("content") or []
        texts: List[str] = []
        artifacts: List[Dict] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "").lower()
            if btype == "text":
                texts.append(str(block.get("text") or ""))
            elif btype in ("image", "resource"):
                artifacts.append(block)
        text_joined = "\n".join(t for t in texts if t.strip())
        return PluginResult(
            success=not is_error,
            data={"text": text_joined, "raw": raw} if text_joined else {"raw": raw},
            error=("MCP tool returned isError=true" if is_error else None),
            artifacts=artifacts,
        )


async def register_mcp_tools_as_plugins() -> List[str]:
    """Idempotent: list tools from every running MCP server and register each
    as an Evermind plugin. Returns the list of plugin names registered."""
    try:
        import mcp_client
        reg = mcp_client.registry()
        all_tools = await reg.list_all_tools()
    except Exception as exc:
        logger.warning("MCP plugin registration: cannot list tools: %s", exc)
        return []

    registered: List[str] = []
    for server_name, tools in all_tools.items():
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_name = str(tool.get("name") or "").strip()
            if not tool_name:
                continue
            plugin_name = _normalize_plugin_name(server_name, tool_name)
            if PluginRegistry.get(plugin_name):
                continue  # already registered
            plugin = MCPToolPlugin(
                server_name=server_name,
                tool_name=tool_name,
                description=str(tool.get("description") or "")[:600],
                parameters_schema=tool.get("inputSchema") or tool.get("input_schema") or {
                    "type": "object", "properties": {}, "required": [],
                },
            )
            PluginRegistry.register(plugin)
            registered.append(plugin_name)
    if registered:
        logger.info("[MCP] Registered %d MCP tool(s) as plugins: %s", len(registered), registered[:8])
    return registered


__all__ = ["MCPToolPlugin", "register_mcp_tools_as_plugins"]
