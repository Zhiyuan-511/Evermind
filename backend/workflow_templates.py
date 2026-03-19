"""
Evermind — Built-in Workflow Templates

Defines reusable node sequences aligned with the Orchestrator's plan decomposition.
Each template maps to a difficulty level (simple/standard/pro) and specifies
which NodeExecutions to auto-create when launching a run.
"""

from typing import Dict, List, Optional

# ─────────────────────────────────────────────
# Built-in Templates
# ─────────────────────────────────────────────
BUILT_IN_TEMPLATES: Dict[str, Dict] = {
    "simple": {
        "id": "simple",
        "label": "Simple (3 nodes)",
        "description": "Fast mode: builder → deployer → tester",
        "nodes": [
            {"key": "builder",  "label": "Builder",  "depends_on": []},
            {"key": "deployer", "label": "Deployer", "depends_on": ["builder"]},
            {"key": "tester",   "label": "Tester",   "depends_on": ["deployer"]},
        ],
    },
    "standard": {
        "id": "standard",
        "label": "Standard (4 nodes)",
        "description": "Balanced mode: builder → reviewer + deployer → tester",
        "nodes": [
            {"key": "builder",  "label": "Builder",  "depends_on": []},
            {"key": "reviewer", "label": "Reviewer", "depends_on": ["builder"]},
            {"key": "deployer", "label": "Deployer", "depends_on": ["builder"]},
            {"key": "tester",   "label": "Tester",   "depends_on": ["deployer"]},
        ],
    },
    "pro": {
        "id": "pro",
        "label": "Pro (7 nodes)",
        "description": "Advanced mode: analyst → 2 builders → reviewer + deployer → tester → debugger",
        "nodes": [
            {"key": "analyst",  "label": "Analyst",   "depends_on": []},
            {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst"]},
            {"key": "builder2", "label": "Builder 2", "depends_on": ["analyst"]},
            {"key": "reviewer", "label": "Reviewer",  "depends_on": ["builder1", "builder2"]},
            {"key": "deployer", "label": "Deployer",  "depends_on": ["builder1", "builder2"]},
            {"key": "tester",   "label": "Tester",    "depends_on": ["reviewer", "deployer"]},
            {"key": "debugger", "label": "Debugger",  "depends_on": ["tester"]},
        ],
    },
}

# Map difficulty aliases to template IDs (for backward compat)
_DIFFICULTY_ALIAS: Dict[str, str] = {
    "simple": "simple",
    "standard": "standard",
    "pro": "pro",
    "fast": "simple",
    "balanced": "standard",
    "advanced": "pro",
}


def get_template(template_id: str) -> Optional[Dict]:
    """Look up a template by ID or difficulty alias."""
    normalized = _DIFFICULTY_ALIAS.get(template_id, template_id)
    return BUILT_IN_TEMPLATES.get(normalized)


def list_templates() -> List[Dict]:
    """Return summary list of all available templates."""
    return [
        {
            "id": t["id"],
            "label": t["label"],
            "description": t.get("description", ""),
            "nodeCount": len(t["nodes"]),
        }
        for t in BUILT_IN_TEMPLATES.values()
    ]


def template_nodes(template_id: str) -> List[Dict]:
    """Return the node definitions for a template, or empty list if not found."""
    tpl = get_template(template_id)
    return list(tpl["nodes"]) if tpl else []
