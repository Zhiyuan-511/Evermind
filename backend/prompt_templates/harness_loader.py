"""
Evermind Harness Template Loader
================================
Loads agent harness definitions from YAML template files.

Provides a single entry point: load_agent_presets() which returns a dict
compatible with the existing AGENT_PRESETS format used by ai_bridge.py.

Architecture:
  - Template files live in prompt_templates/*.yaml alongside this module
  - Each YAML file defines one node type with metadata + instructions
  - Python code can override or extend loaded templates for custom nodes
  - Hot-reload supported via load_agent_presets(force_reload=True)

Template YAML schema:
  name: str           # Node type identifier (must match filename stem)
  category: str       # orchestration | core | quality | support | art
  complexity: str     # template (prompt-only) | custom (has Python logic)
  tools: list[str]    # Available tool categories for this node
  description: str    # One-line description for UI/logging
  instructions: str   # The system prompt / harness text
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("evermind.harness_loader")

# ── Module-level cache ──
_TEMPLATES_DIR = Path(__file__).parent
_CACHED_PRESETS: Optional[Dict[str, Dict[str, Any]]] = None
_CACHED_METADATA: Optional[Dict[str, Dict[str, Any]]] = None


def _parse_yaml_simple(text: str) -> Dict[str, Any]:
    """Lightweight YAML-subset parser for harness templates.

    Handles the specific schema used by Evermind templates without
    requiring the PyYAML dependency. Supports:
      - Scalar fields (name, category, complexity, description)
      - Simple list fields (tools)
      - Multi-line block scalars (instructions with | indicator)

    This avoids adding PyYAML as a dependency while still allowing
    human-readable template files.
    """
    result: Dict[str, Any] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip comments and empty lines
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Parse key: value
        if ":" in stripped:
            colon_pos = stripped.index(":")
            key = stripped[:colon_pos].strip()
            value_part = stripped[colon_pos + 1:].strip()

            if value_part == "|":
                # Block scalar — collect indented lines
                block_lines = []
                i += 1
                if i < len(lines):
                    # Detect indentation of first content line
                    first_content = lines[i]
                    indent = len(first_content) - len(first_content.lstrip())
                    if indent == 0 and first_content.strip():
                        # No indentation detected, try 2 spaces
                        indent = 2
                    while i < len(lines):
                        bline = lines[i]
                        # Stop at non-indented, non-empty line (new key)
                        if bline.strip() and not bline.startswith(" " * max(indent, 1)):
                            break
                        # Strip the block indentation
                        if len(bline) >= indent:
                            block_lines.append(bline[indent:])
                        else:
                            block_lines.append(bline.lstrip())
                        i += 1
                # Strip trailing empty lines
                while block_lines and not block_lines[-1].strip():
                    block_lines.pop()
                result[key] = "\n".join(block_lines)
                continue

            elif value_part == "" or value_part == "[]":
                # Could be empty value or start of a list
                if value_part == "[]":
                    result[key] = []
                    i += 1
                    continue
                # Check if next lines are list items
                peek = i + 1
                if peek < len(lines) and lines[peek].strip().startswith("- "):
                    # It's a list
                    items = []
                    i += 1
                    while i < len(lines):
                        item_line = lines[i].strip()
                        if item_line.startswith("- "):
                            items.append(item_line[2:].strip())
                            i += 1
                        elif not item_line or item_line.startswith("#"):
                            i += 1
                        else:
                            break
                    result[key] = items
                    continue
                else:
                    result[key] = ""
                    i += 1
                    continue
            else:
                # Simple scalar value
                result[key] = value_part
                i += 1
                continue

        i += 1

    return result


def _load_single_template(filepath: Path) -> Optional[Dict[str, Any]]:
    """Load and validate a single YAML template file."""
    try:
        raw = filepath.read_text(encoding="utf-8")
        parsed = _parse_yaml_simple(raw)

        if "instructions" not in parsed:
            logger.warning("Template %s missing 'instructions' field, skipped", filepath.name)
            return None

        return parsed
    except Exception as exc:
        logger.error("Failed to load template %s: %s", filepath.name, exc)
        return None


def load_agent_presets(force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    """Load all harness templates and return AGENT_PRESETS-compatible dict.

    Returns:
        Dict mapping node_type -> {"instructions": str}
        Compatible with the existing AGENT_PRESETS format in ai_bridge.py.
    """
    global _CACHED_PRESETS, _CACHED_METADATA

    if _CACHED_PRESETS is not None and not force_reload:
        return _CACHED_PRESETS

    presets: Dict[str, Dict[str, Any]] = {}
    metadata: Dict[str, Dict[str, Any]] = {}

    yaml_files = sorted(_TEMPLATES_DIR.glob("*.yaml"))
    if not yaml_files:
        logger.warning("No YAML templates found in %s", _TEMPLATES_DIR)
        return presets

    for yf in yaml_files:
        parsed = _load_single_template(yf)
        if parsed is None:
            continue

        node_name = parsed.get("name", yf.stem)

        # Build AGENT_PRESETS-compatible entry (only "instructions")
        presets[node_name] = {
            "instructions": parsed["instructions"],
        }

        # Store metadata separately for introspection
        metadata[node_name] = {
            "category": parsed.get("category", "unknown"),
            "complexity": parsed.get("complexity", "template"),
            "tools": parsed.get("tools", []),
            "description": parsed.get("description", ""),
            "source_file": str(yf),
        }

    loaded_count = len(presets)
    template_count = sum(1 for m in metadata.values() if m["complexity"] == "template")
    custom_count = loaded_count - template_count

    logger.info(
        "Loaded %d harness templates (%d template-only, %d custom-logic) from %s",
        loaded_count, template_count, custom_count, _TEMPLATES_DIR,
    )

    _CACHED_PRESETS = presets
    _CACHED_METADATA = metadata
    return presets


def get_template_metadata() -> Dict[str, Dict[str, Any]]:
    """Return metadata for all loaded templates.

    Call load_agent_presets() first to populate the cache.
    """
    if _CACHED_METADATA is None:
        load_agent_presets()
    return _CACHED_METADATA or {}


def get_node_category(node_type: str) -> str:
    """Return the category for a node type (orchestration/core/quality/support/art)."""
    meta = get_template_metadata()
    return meta.get(node_type, {}).get("category", "unknown")


def get_node_complexity(node_type: str) -> str:
    """Return complexity level (template/custom) for a node type."""
    meta = get_template_metadata()
    return meta.get(node_type, {}).get("complexity", "template")


def is_template_only(node_type: str) -> bool:
    """Check if a node type is template-only (no custom Python logic)."""
    return get_node_complexity(node_type) == "template"


def list_nodes_by_category() -> Dict[str, list]:
    """Group all loaded node types by category."""
    meta = get_template_metadata()
    groups: Dict[str, list] = {}
    for node_type, info in meta.items():
        cat = info.get("category", "unknown")
        groups.setdefault(cat, []).append(node_type)
    return groups
