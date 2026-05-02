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

from node_roles import normalize_node_role

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
    # V4.3.1: All pipeline nodes get file_ops + shell (Claude Code-style agentic tools).
    # v6.5 Phase 2 (#13): builders opt into source_fetch so they can chase the
    # analyst's <follow_hints> entries (README/docs/source-code snippets) on
    # demand without a rebuild round-trip. Guarded by analyst-set per-tool
    # limits; a cold goal with no hints never fires a fetch.
    "builder":   ["file_ops", "shell", "source_fetch"],
    "polisher":  ["file_ops", "shell"],
    # v6.4 (maintainer): patcher had NO plugins registered → every run
    # the patcher node was launched with zero tools. It tried to output
    # unified-diff blocks in plain text which kimi/gpt-5.4 reliably refuse
    # to do, wasting ~4 min per rejection. Giving it file_ops + shell (same
    # as polisher) lets it actually read & edit the flagged files.
    "patcher":   ["file_ops", "shell"],
    # v6.4.6 (maintainer): merger was ALSO unregistered like patcher.
    # It currently works only because orchestrator creates it with
    # agent_type="builder" so it inherits builder plugins at runtime — but
    # that's a fragile accident. Register explicitly for stability.
    "merger":    ["file_ops", "shell"],
    "tester":    ["file_ops", "shell", "browser"],
    "reviewer":  ["file_ops", "shell", "browser"],
    "deployer":  ["file_ops", "shell"],
    "debugger":  ["file_ops", "shell"],
    # v6.1.2: analyst drops `browser` and `shell` — it's a text-research node
    # that only needs source_fetch (raw HTML/markdown from GitHub/docs/raw).
    # Giving it a rendered browser caused two regressions: (1) on CDP-attach
    # failure Playwright spawned a visible external Chromium window during
    # long-running GitHub research, (2) the model wasted tool-loop rounds
    # choosing between source_fetch and browser when source_fetch is strictly
    # sufficient for README/source inspection. file_ops is also unneeded —
    # analyst MUST produce a text report, never write files (enforced by
    # _plain_text_node_write_guard_error). Reviewer/tester keep browser for
    # QA screenshots. uidesign defaults to text-only and may opt into browser
    # explicitly when live reference capture is worth the latency.
    "analyst":   ["source_fetch"],
    "scribe":    ["file_ops", "shell"],
    # v5.8.6: planner.yaml HARD RULE says "DO NOT call any tools". Giving it
    # file_ops + shell here contradicted that — kimi would try tool calls
    # anyway, waste 4-5 tool_loop iterations at 2-3s each before converging
    # on plain-text output. Empty plugin list makes the contract machine-enforced.
    "planner":   [],
    "spritesheet": ["file_ops"],
    "assetimport": ["file_ops"],
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
    "imagegen":    ["file_ops", "comfyui"],
    "bgremove":    [],
    "videoedit":   [],
    "uidesign":    [],
    # v5.1: spritesheet/assetimport now produce code files — moved above with file_ops
}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _get_config_value(config: Optional[Dict[str, Any]], *paths: str) -> Any:
    if not isinstance(config, dict):
        return None
    for path in paths:
        current: Any = config
        found = True
        for part in str(path).split("."):
            if isinstance(current, dict) and part in current:
                current = current.get(part)
            else:
                found = False
                break
        if found:
            return current
    return None


def _get_config_string(config: Optional[Dict[str, Any]], *paths: str) -> str:
    value = _get_config_value(config, *paths)
    if value is None:
        return ""
    return str(value).strip()


def get_image_generation_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """
    Resolve the configured image-generation backend without assuming a default local
    ComfyUI instance is actually available.
    """
    comfyui_url = _get_config_string(
        config,
        "comfyui_base_url",
        "comfyui_url",
        "image_generation.comfyui_url",
        "image_generation.base_url",
    )
    workflow_template = _get_config_string(
        config,
        "comfyui_workflow_template",
        "comfyui_template_path",
        "image_generation.workflow_template",
        "image_generation.comfyui_workflow_template",
    )
    if not comfyui_url:
        comfyui_url = str(os.getenv("EVERMIND_COMFYUI_URL", "") or "").strip()
    if not workflow_template:
        workflow_template = str(os.getenv("EVERMIND_COMFYUI_WORKFLOW_TEMPLATE", "") or "").strip()
    return {
        "comfyui_url": comfyui_url.rstrip("/"),
        "workflow_template": workflow_template,
    }


def is_video_review_available(config: Optional[Dict[str, Any]] = None) -> bool:
    """v6.2 (maintainer): VideoReview adapter is usable when a vision
    model is configured (qwen-vl via DashScope, doubao-vision via Volcengine,
    or Gemini). Also triggers on explicit video_review.api_key."""
    try:
        from video_review import is_video_review_available as _probe
    except Exception:
        return False
    try:
        return bool(_probe(config))
    except Exception:
        return False


def is_direct_image_provider_configured(config: Optional[Dict[str, Any]] = None) -> bool:
    """v6.2 (maintainer): True iff image_generation has both provider AND api_key.
    Used to branch imagegen node defaults between the preferred direct adapter
    (ImageGenPlugin) and the legacy ComfyUI plugin."""
    enabled_override = _get_config_value(config, "image_generation.enabled", "enable_image_generation")
    if enabled_override is not None and not _is_truthy(enabled_override):
        return False
    if isinstance(config, dict):
        ig = config.get("image_generation") if isinstance(config.get("image_generation"), dict) else {}
        if str(ig.get("provider") or "").strip() and str(ig.get("api_key") or "").strip():
            return True
    return False


def is_image_generation_available(config: Optional[Dict[str, Any]] = None) -> bool:
    """
    Only treat image generation as available when EITHER:
    (1) v6.1.15 (maintainer): Direct provider+api_key configured (tongyi /
        doubao / flux-fal / openai-compat / ...). Path: image_generation.provider
        + image_generation.api_key.
    (2) Legacy: ComfyUI URL + workflow template configured.
    Kimi text models do NOT count as image capability.
    """
    if is_direct_image_provider_configured(config=config):
        return True
    enabled_override = _get_config_value(config, "image_generation.enabled", "enable_image_generation")
    if enabled_override is not None and not _is_truthy(enabled_override):
        return False
    # Path 2: legacy ComfyUI
    image_cfg = get_image_generation_config(config=config)
    return bool(image_cfg["comfyui_url"] and image_cfg["workflow_template"])


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


def is_polisher_browser_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """
    Optional flag for letting polisher browse during refinement.
    Default is OFF because browser-first behavior caused repeated non-productive
    tool loops; polisher should primarily patch local HTML/CSS/JS directly.
    """
    if isinstance(config, dict):
        for key in ("polisher_enable_browser", "polisher_browser_enabled", "enable_polisher_browser"):
            if key in config:
                return _is_truthy(config.get(key))
        nested_polisher = config.get("polisher")
        if isinstance(nested_polisher, dict) and "enable_browser_search" in nested_polisher:
            return _is_truthy(nested_polisher.get("enable_browser_search"))
    return _is_truthy(os.getenv("EVERMIND_POLISHER_ENABLE_BROWSER", "0"))


def is_uidesign_browser_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """
    Optional flag for letting uidesign inspect a live reference site.
    Default is OFF because broad browser research turned uidesign into a slow
    pseudo-analyst node instead of a compact builder-facing design brief.
    """
    if isinstance(config, dict):
        for key in ("uidesign_enable_browser", "uidesign_browser_enabled", "enable_uidesign_browser"):
            if key in config:
                return _is_truthy(config.get(key))
        nested_uidesign = config.get("uidesign")
        if isinstance(nested_uidesign, dict) and "enable_browser_search" in nested_uidesign:
            return _is_truthy(nested_uidesign.get("enable_browser_search"))
    return _is_truthy(os.getenv("EVERMIND_UIDESIGN_ENABLE_BROWSER", "0"))


def is_imagegen_browser_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """
    When no real image backend is configured, imagegen should pivot to sourcing /
    design-research mode instead of exposing a dead comfyui tool.
    """
    if is_image_generation_available(config=config):
        return False
    if isinstance(config, dict):
        for key in ("imagegen_enable_browser", "imagegen_browser_enabled", "enable_imagegen_browser"):
            if key in config:
                return _is_truthy(config.get(key))
        nested_imagegen = config.get("imagegen")
        if isinstance(nested_imagegen, dict) and "enable_browser_search" in nested_imagegen:
            return _is_truthy(nested_imagegen.get("enable_browser_search"))
    return _is_truthy(os.getenv("EVERMIND_IMAGEGEN_ENABLE_BROWSER", "1"))


def is_browser_runtime_available(config: Optional[Dict[str, Any]] = None) -> bool:
    """
    Runtime availability signal for Playwright-backed browser flows.
    Defaults to True unless an explicit runtime probe marks it unavailable.
    """
    explicit = _get_config_value(
        config,
        "playwright_available",
        "browser_runtime_available",
        "runtime.playwright_available",
    )
    if explicit is None:
        return True
    return _is_truthy(explicit)


def is_browser_use_runtime_available(config: Optional[Dict[str, Any]] = None) -> bool:
    explicit = _get_config_value(
        config,
        "browser_use_runtime_available",
        "runtime.browser_use_available",
    )
    if explicit is not None:
        return _is_truthy(explicit)
    return is_browser_runtime_available(config)


def is_qa_computer_use_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """
    Optional escalation path for reviewer/tester when browser-only validation is insufficient.
    Stays OFF by default to avoid adding brittle tool choices when OpenAI CUA is unavailable.
    """
    if isinstance(config, dict):
        for key in ("qa_enable_computer_use", "reviewer_tester_enable_computer_use"):
            if key in config:
                enabled = _is_truthy(config.get(key))
                break
        else:
            enabled = _is_truthy(os.getenv("EVERMIND_QA_ENABLE_COMPUTER_USE", "0"))
    else:
        enabled = _is_truthy(os.getenv("EVERMIND_QA_ENABLE_COMPUTER_USE", "0"))
    if not enabled:
        return False
    openai_key = ""
    if isinstance(config, dict):
        openai_key = str(config.get("openai_api_key", "") or "")
    if not openai_key:
        openai_key = str(os.getenv("OPENAI_API_KEY", "") or "")
    return bool(openai_key.strip())


def is_qa_browser_use_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """
    Optional higher-level browser agent for reviewer/tester when the product
    requires multi-step interaction such as web apps or browser games.
    Kept OFF by default because browser-use is an optional sidecar dependency.
    """
    if isinstance(config, dict):
        for key in ("qa_enable_browser_use", "reviewer_tester_enable_browser_use"):
            if key in config:
                enabled = _is_truthy(config.get(key))
                break
        else:
            enabled = _is_truthy(os.getenv("EVERMIND_QA_ENABLE_BROWSER_USE", "0"))
    else:
        enabled = _is_truthy(os.getenv("EVERMIND_QA_ENABLE_BROWSER_USE", "0"))
    if not enabled:
        return False
    openai_key = ""
    if isinstance(config, dict):
        openai_key = str(config.get("openai_api_key", "") or "")
        if not openai_key:
            api_keys = config.get("api_keys")
            if isinstance(api_keys, dict):
                openai_key = str(api_keys.get("openai", "") or "")
    if not openai_key:
        openai_key = str(os.getenv("OPENAI_API_KEY", "") or "")
    return bool(openai_key.strip())


def get_default_plugins_for_node(node_type: str, config: Optional[Dict[str, Any]] = None) -> List[str]:
    normalized_node_type = normalize_node_role(node_type)
    if normalized_node_type == "imagegen":
        defaults = ["file_ops"]
        # v6.2 (maintainer): prefer the direct ImageGen adapter when provider+key set.
        # Fall back to ComfyUI for legacy users. No-image → browser research mode.
        if is_direct_image_provider_configured(config=config):
            defaults.append("image_gen")
        elif is_image_generation_available(config=config):
            defaults.append("comfyui")
        elif is_imagegen_browser_enabled(config=config):
            defaults.append("source_fetch")
            defaults.append("browser")
    else:
        defaults = list(NODE_DEFAULT_PLUGINS.get(normalized_node_type, []))
    if normalized_node_type == "builder" and is_builder_browser_enabled(config=config):
        if "browser" not in defaults:
            defaults.append("browser")
    if normalized_node_type == "polisher" and is_polisher_browser_enabled(config=config):
        if "browser" not in defaults:
            defaults.append("browser")
    if normalized_node_type == "uidesign" and is_uidesign_browser_enabled(config=config):
        if "browser" not in defaults:
            defaults.append("browser")
    if normalized_node_type in ("reviewer", "tester") and is_qa_browser_use_enabled(config=config):
        if "browser_use" not in defaults:
            insert_at = 1 if "file_ops" in defaults else 0
            defaults.insert(insert_at, "browser_use")
    if normalized_node_type in ("reviewer", "tester") and is_qa_computer_use_enabled(config=config):
        if "computer_use" not in defaults:
            defaults.append("computer_use")
    # v6.2 (maintainer): reviewer/tester may opt into video review when
    # a vision model is reachable. Adapter returns None on failure so the flow
    # silently falls back to screenshot-based review.
    if normalized_node_type == "reviewer" and is_video_review_available(config=config):
        if "video_review" not in defaults:
            defaults.append("video_review")
    if "browser" in defaults and not is_browser_runtime_available(config=config):
        defaults = [name for name in defaults if name != "browser"]
    if "browser_use" in defaults and not is_browser_use_runtime_available(config=config):
        defaults = [name for name in defaults if name != "browser_use"]
    return defaults


def sanitize_plugin_names_for_node(
    node_type: str,
    plugin_names: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    normalized_node_type = normalize_node_role(node_type)
    sanitized: List[str] = []
    seen: set[str] = set()
    for raw_name in plugin_names or []:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if normalized_node_type == "builder" and name == "browser" and not is_builder_browser_enabled(config=config):
            continue
        if normalized_node_type == "polisher" and name == "browser" and not is_polisher_browser_enabled(config=config):
            continue
        if normalized_node_type == "uidesign" and name == "browser" and not is_uidesign_browser_enabled(config=config):
            continue
        if name == "browser" and not is_browser_runtime_available(config=config):
            continue
        if (
            normalized_node_type == "imagegen"
            and name in {"browser", "source_fetch"}
            and not is_imagegen_browser_enabled(config=config)
        ):
            continue
        if normalized_node_type == "imagegen" and name == "comfyui" and not is_image_generation_available(config=config):
            continue
        if normalized_node_type == "imagegen" and name == "image_gen" and not is_direct_image_provider_configured(config=config):
            continue
        if name == "video_review" and not is_video_review_available(config=config):
            continue
        if normalized_node_type in ("reviewer", "tester") and name == "browser_use" and (
            not is_qa_browser_use_enabled(config=config)
            or not is_browser_use_runtime_available(config=config)
        ):
            continue
        if normalized_node_type in ("reviewer", "tester") and name == "computer_use" and not is_qa_computer_use_enabled(config=config):
            continue
        sanitized.append(name)
    return sanitized


def resolve_enabled_plugins_for_node(
    node_type: str,
    *,
    explicit_plugins: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    normalized_node_type = normalize_node_role(node_type)
    defaults = get_default_plugins_for_node(node_type, config=config)
    has_explicit_plugins = any(str(item or "").strip() for item in (explicit_plugins or []))
    if not has_explicit_plugins:
        return defaults

    resolved = sanitize_plugin_names_for_node(node_type, explicit_plugins, config=config)
    if "file_ops" in defaults and "file_ops" not in resolved:
        resolved.insert(0, "file_ops")
    if normalized_node_type == "imagegen":
        for required in defaults:
            if required != "file_ops" and required not in resolved:
                resolved.append(required)
    return resolved


def get_effective_default_plugins(config: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    return {
        node_type: get_default_plugins_for_node(node_type, config=config)
        for node_type in NODE_DEFAULT_PLUGINS.keys()
    }
