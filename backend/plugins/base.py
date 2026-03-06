"""
Evermind Backend — Plugin Base Classes & Registry
Each plugin represents a capability that can be attached to AI agent nodes.
"""

import asyncio
import logging
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
        defaults = NODE_DEFAULT_PLUGINS.get(node_type, [])
        return [cls._plugins[n] for n in defaults if n in cls._plugins]


# Default plugin assignments per node type
NODE_DEFAULT_PLUGINS = {
    "builder":   ["file_ops", "shell", "git", "computer_use"],
    "tester":    ["screenshot", "browser", "shell", "computer_use"],
    "reviewer":  ["screenshot", "browser", "computer_use"],
    "deployer":  ["shell", "git", "browser"],
    "debugger":  ["screenshot", "file_ops", "shell", "computer_use"],
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
