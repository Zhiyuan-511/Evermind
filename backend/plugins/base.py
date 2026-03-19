"""
Evermind Backend — Plugin Base Classes & Registry
Each plugin represents a capability that can be attached to AI agent nodes.
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from enum import Enum

logger = logging.getLogger("evermind.plugins")


class SecurityLevel(str, Enum):
    L1 = "L1"  # Read-only, no confirmation
    L2 = "L2"  # File/Network, auto-approve configurable
    L3 = "L3"  # Confirm required before execution
    L4 = "L4"  # Password + countdown


class PluginResult:
    """Standardized result from plugin execution."""
    def __init__(self, success: bool, data: Any = None, error: str = None,
                 artifacts: List[Dict] = None):
        self.success = success
        self.data = data
        self.error = error
        self.artifacts = artifacts or []  # screenshots, files, etc.

    def to_dict(self):
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "artifacts": self.artifacts
        }


class Plugin(ABC):
    """Base class for all Evermind plugins."""
    name: str = ""
    display_name: str = ""
    description: str = ""
    icon: str = ""
    security_level: SecurityLevel = SecurityLevel.L1

    @abstractmethod
    async def execute(self, params: Dict[str, Any], context: Dict = None) -> PluginResult:
        """Execute the plugin action with given parameters."""
        raise NotImplementedError

    def get_tool_definition(self) -> Dict:
        """Return OpenAI function-calling compatible tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._get_parameters_schema()
            }
        }

    def _get_parameters_schema(self) -> Dict:
        """Override to define parameter schema for AI tool calling."""
        return {"type": "object", "properties": {}, "required": []}


class PluginRegistry:
    """Global registry of available plugins."""
    _plugins: Dict[str, Plugin] = {}

    @classmethod
    def register(cls, plugin: Plugin):
        cls._plugins[plugin.name] = plugin
        logger.info(f"Registered plugin: {plugin.name} ({plugin.security_level.value})")

    @classmethod
    def get(cls, name: str) -> Optional[Plugin]:
        return cls._plugins.get(name)

    @classmethod
    def get_all(cls) -> Dict[str, Plugin]:
        return cls._plugins.copy()

    @classmethod
    def get_for_node(cls, node_type: str) -> List[Plugin]:
        """Return recommended plugins for a given node type."""
        defaults = get_default_plugins_for_node(node_type)
        return [cls._plugins[n] for n in defaults if n in cls._plugins]


# Default plugin assignments per node type
NODE_DEFAULT_PLUGINS = {
    # Keep autonomous web pipeline stable: prioritize deterministic local file ops.
    "builder":   ["file_ops"],
    "tester":    ["file_ops", "screenshot", "browser"],
    "reviewer":  ["file_ops", "screenshot", "browser"],
    "deployer":  ["file_ops"],
    "debugger":  ["file_ops", "shell"],
    "analyst":   ["file_ops", "browser"],
    "scribe":    ["file_ops"],
    "planner":   [],
    "router":    [],
    # Local execution nodes — these have their own built-in capabilities
    "localshell":  ["shell"],
    "fileread":    ["file_ops"],
    "filewrite":   ["file_ops"],
    "screenshot":  ["screenshot"],
    "gitops":      ["git"],
    "browser":     ["browser"],
    "uicontrol":   ["ui_control"],
    # Art nodes
    "imagegen":    [],
    "bgremove":    [],
    "videoedit":   [],
    "uidesign":    ["screenshot", "browser"],
    "spritesheet": ["file_ops"],
    "assetimport": ["file_ops"],
}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def is_builder_browser_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """
    Feature flag for letting builder use the browser plugin for style/web research.
    Default is OFF to keep the local pipeline deterministic.
    """
    if isinstance(config, dict):
        for key in ("builder_enable_browser", "builder_browser_enabled", "enable_builder_browser"):
            if key in config:
                return _is_truthy(config.get(key))
        nested_builder = config.get("builder")
        if isinstance(nested_builder, dict) and "enable_browser_search" in nested_builder:
            return _is_truthy(nested_builder.get("enable_browser_search"))
    return _is_truthy(os.getenv("EVERMIND_BUILDER_ENABLE_BROWSER", "0"))


def get_default_plugins_for_node(node_type: str, config: Optional[Dict[str, Any]] = None) -> List[str]:
    defaults = list(NODE_DEFAULT_PLUGINS.get(node_type, []))
    if node_type == "builder" and is_builder_browser_enabled(config=config):
        if "browser" not in defaults:
            defaults.append("browser")
    return defaults


def get_effective_default_plugins(config: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    return {
        node_type: get_default_plugins_for_node(node_type, config=config)
        for node_type in NODE_DEFAULT_PLUGINS.keys()
    }
