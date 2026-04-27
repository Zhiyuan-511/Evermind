from __future__ import annotations

import re


_CANONICAL_NODE_ROLES = {
    "router",
    "planner",
    "analyst",
    "builder",
    "polisher",
    "patcher",
    "reviewer",
    "tester",
    "deployer",
    "debugger",
    "scribe",
    "uidesign",
    "imagegen",
    "spritesheet",
    "assetimport",
    "localshell",
    "fileread",
    "filewrite",
    "screenshot",
    "gitops",
    "browser",
    "uicontrol",
    "bgremove",
    "videoedit",
    "merger",
    "monitor",
}

_ROLE_ALIASES = {
    "builder1": "builder",
    "builder2": "builder",
    "builder_structure": "builder",
    "builder_ui": "builder",
    "builder_copy": "builder",
    "builder_animation": "builder",
    "builder_responsive": "builder",
    "reviewer_design": "reviewer",
    "reviewer_code": "reviewer",
    "tester_ui": "tester",
    "tester_smoke": "tester",
}


def normalize_node_role(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in _ROLE_ALIASES:
        return _ROLE_ALIASES[raw]
    if raw in _CANONICAL_NODE_ROLES:
        return raw

    without_numeric_suffix = re.sub(r"(?:[_-]?\d+)+$", "", raw)
    if without_numeric_suffix in _ROLE_ALIASES:
        return _ROLE_ALIASES[without_numeric_suffix]
    if without_numeric_suffix in _CANONICAL_NODE_ROLES:
        return without_numeric_suffix

    alpha_prefix = re.match(r"^[a-z]+", raw)
    if alpha_prefix:
        prefix = alpha_prefix.group(0)
        if prefix in _CANONICAL_NODE_ROLES:
            return prefix

    for canonical in (
        "builder",
        "polisher",
        "patcher",
        "reviewer",
        "tester",
        "deployer",
        "debugger",
        "analyst",
        "scribe",
        "uidesign",
        "imagegen",
        "spritesheet",
        "assetimport",
        "planner",
        "router",
    ):
        if raw.startswith(f"{canonical}_") or raw.startswith(f"{canonical}-"):
            return canonical

    return without_numeric_suffix or raw
