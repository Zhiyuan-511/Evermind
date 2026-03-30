"""
Evermind — Built-in Workflow Templates

Defines reusable node sequences aligned with the Orchestrator's plan decomposition.
Each template maps to a difficulty level (simple/standard/pro) and specifies
which NodeExecutions to auto-create when launching a run.
"""

import re
from typing import Any, Dict, List, Optional

import task_classifier

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
        "label": "Pro (7-10 nodes)",
        "description": "Deep mode: app-selected 7-10 node chain based on goal complexity",
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

_PRO_VISUAL_COMPLEXITY_RE = re.compile(
    r"(动画|animation|转场|cinematic|电影|movie-like|motion|scrolltrigger|gsap|parallax|immersive|"
    r"高端|高级|奢侈|luxury|apple-style|像苹果|editorial|brand film|视觉特效|视觉效果)",
    re.IGNORECASE,
)
_PRO_CONTENT_COMPLEXITY_RE = re.compile(
    r"(品牌故事|故事线|story|heritage|craft|工艺|系列|collection|lookbook|pricing|faq|docs|documentation|"
    r"case stud|timeline|chapter|compare|comparison|spec|information architecture|站点地图|内容架构)",
    re.IGNORECASE,
)
_PRO_ASSET_HEAVY_RE = re.compile(
    r"(插画|illustration|hero image|lookbook|封面|海报|poster|concept art|storyboard|素材包|asset pack|render)",
    re.IGNORECASE,
)


def pro_template_profile(goal: str = "") -> Dict[str, Any]:
    """Return the deep-mode node strategy for a goal.

    Deep mode stays deterministic, but the app can expand from 7 up to 10 nodes
    when the brief clearly needs stronger design direction, content planning,
    or an explicit asset pipeline.
    """
    text = str(goal or "").strip()
    task_type = task_classifier.classify(text).task_type if text else "website"
    page_count = task_classifier.requested_page_count(text) if text else 1
    multi_page = task_classifier.wants_multi_page(text) if text else False
    visual_complex = bool(_PRO_VISUAL_COMPLEXITY_RE.search(text))
    content_complex = bool(_PRO_CONTENT_COMPLEXITY_RE.search(text))
    asset_heavy = bool(_PRO_ASSET_HEAVY_RE.search(text)) or task_classifier.wants_generated_assets(text)
    architecture_complex = task_type in {"game", "dashboard", "presentation", "creative"}
    long_brief = len(text) >= 140
    include_polisher = (
        not asset_heavy
        and task_type in {"website", "dashboard", "presentation", "creative"}
        and (visual_complex or multi_page or content_complex or long_brief)
    )

    if asset_heavy:
        return {
            "node_count": 10,
            "include_uidesign": False,
            "include_scribe": False,
            "include_asset_pipeline": True,
            "include_polisher": False,
            "parallel_builders": True,
            "reason": "asset_heavy",
        }

    needs_uidesign = multi_page or visual_complex or long_brief or (
        architecture_complex and (content_complex or len(text) >= 80)
    )
    needs_scribe = (
        (multi_page and page_count >= 6)
        or content_complex
        or (architecture_complex and long_brief)
    )
    large_multi_page_website = task_type == "website" and multi_page and page_count >= 6

    if needs_uidesign and needs_scribe:
        parallel_builders = large_multi_page_website
        return {
            "node_count": (
                10 if include_polisher else 9
            ) if parallel_builders else (
                9 if include_polisher else 8
            ),
            "include_uidesign": True,
            "include_scribe": True,
            "include_asset_pipeline": False,
            "include_polisher": include_polisher,
            "parallel_builders": parallel_builders,
            "scribe_blocks_builders": not parallel_builders,
            "reason": (
                "design_and_content_complexity_parallelized"
                if parallel_builders else
                "design_and_content_complexity"
            ),
        }

    if needs_uidesign:
        return {
            "node_count": 9 if include_polisher else 8,
            "include_uidesign": True,
            "include_scribe": False,
            "include_asset_pipeline": False,
            "include_polisher": include_polisher,
            "parallel_builders": True,
            "reason": "design_complexity",
        }

    return {
        "node_count": 8 if include_polisher else 7,
        "include_uidesign": False,
        "include_scribe": False,
        "include_asset_pipeline": False,
        "include_polisher": include_polisher,
        "parallel_builders": True,
        "reason": "baseline_polished" if include_polisher else "baseline",
    }


def _build_pro_template(goal: str = "") -> Dict[str, Any]:
    profile = pro_template_profile(goal)
    include_polisher = bool(profile.get("include_polisher"))
    task_type = task_classifier.classify(str(goal or "")).task_type if str(goal or "").strip() else "website"
    sequential_game_builders = task_type != "website"
    if profile["include_asset_pipeline"]:
        nodes = [
            {"key": "analyst", "label": "Analyst", "depends_on": []},
            {"key": "imagegen", "label": "Image Gen", "depends_on": ["analyst"]},
            {"key": "spritesheet", "label": "Spritesheet", "depends_on": ["analyst", "imagegen"]},
            {"key": "assetimport", "label": "Asset Import", "depends_on": ["analyst", "imagegen", "spritesheet"]},
            {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst", "assetimport"]},
            {"key": "builder2", "label": "Builder 2", "depends_on": (["builder1"] if sequential_game_builders else ["analyst", "assetimport"])},
            {"key": "reviewer", "label": "Reviewer", "depends_on": ["builder1", "builder2"]},
            {"key": "deployer", "label": "Deployer", "depends_on": ["builder1", "builder2"]},
            {"key": "tester", "label": "Tester", "depends_on": ["reviewer", "deployer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["tester"]},
        ]
    elif profile["include_uidesign"] and profile["include_scribe"] and not profile.get("parallel_builders", True):
        nodes = [
            {"key": "analyst", "label": "Analyst", "depends_on": []},
            {"key": "uidesign", "label": "UI Design", "depends_on": ["analyst"]},
            {"key": "scribe", "label": "Scribe", "depends_on": ["analyst"]},
            {"key": "builder", "label": "Builder", "depends_on": ["analyst", "uidesign"]},
        ]
        if include_polisher:
            nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["builder", "scribe"]})
            polish_dep = ["polisher"]
        else:
            polish_dep = ["builder"]
        nodes.extend([
            {"key": "reviewer", "label": "Reviewer", "depends_on": polish_dep},
            {"key": "deployer", "label": "Deployer", "depends_on": polish_dep},
            {"key": "tester", "label": "Tester", "depends_on": ["reviewer", "deployer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["tester"]},
        ])
    elif profile["include_uidesign"] and profile["include_scribe"]:
        scribe_blocks_builders = bool(profile.get("scribe_blocks_builders", True))
        builder_depends_on = ["analyst", "uidesign"]
        if scribe_blocks_builders:
            builder_depends_on.append("scribe")
        nodes = [
            {"key": "analyst", "label": "Analyst", "depends_on": []},
            {"key": "uidesign", "label": "UI Design", "depends_on": ["analyst"]},
            {"key": "scribe", "label": "Scribe", "depends_on": ["analyst"]},
            {"key": "builder1", "label": "Builder 1", "depends_on": builder_depends_on},
            {"key": "builder2", "label": "Builder 2", "depends_on": (["builder1"] if sequential_game_builders else builder_depends_on)},
        ]
        if include_polisher:
            polisher_depends_on = ["builder1", "builder2"]
            if not scribe_blocks_builders:
                polisher_depends_on.append("scribe")
            nodes.append({"key": "polisher", "label": "Polisher", "depends_on": polisher_depends_on})
            polish_dep = ["polisher"]
        else:
            polish_dep = ["builder1", "builder2"]
        nodes.extend([
            {"key": "reviewer", "label": "Reviewer", "depends_on": polish_dep},
            {"key": "deployer", "label": "Deployer", "depends_on": polish_dep},
            {"key": "tester", "label": "Tester", "depends_on": ["reviewer", "deployer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["tester"]},
        ])
    elif profile["include_uidesign"]:
        nodes = [
            {"key": "analyst", "label": "Analyst", "depends_on": []},
            {"key": "uidesign", "label": "UI Design", "depends_on": ["analyst"]},
            {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst", "uidesign"]},
            {"key": "builder2", "label": "Builder 2", "depends_on": (["builder1"] if sequential_game_builders else ["analyst", "uidesign"])},
        ]
        if include_polisher:
            nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["builder1", "builder2"]})
            polish_dep = ["polisher"]
        else:
            polish_dep = ["builder1", "builder2"]
        nodes.extend([
            {"key": "reviewer", "label": "Reviewer", "depends_on": polish_dep},
            {"key": "deployer", "label": "Deployer", "depends_on": polish_dep},
            {"key": "tester", "label": "Tester", "depends_on": ["reviewer", "deployer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["tester"]},
        ])
    else:
        nodes = [
            {"key": "analyst", "label": "Analyst", "depends_on": []},
            {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst"]},
            {"key": "builder2", "label": "Builder 2", "depends_on": (["builder1"] if sequential_game_builders else ["analyst"])},
        ]
        if include_polisher:
            nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["builder1", "builder2"]})
            polish_dep = ["polisher"]
        else:
            polish_dep = ["builder1", "builder2"]
        nodes.extend([
            {"key": "reviewer", "label": "Reviewer", "depends_on": polish_dep},
            {"key": "deployer", "label": "Deployer", "depends_on": polish_dep},
            {"key": "tester", "label": "Tester", "depends_on": ["reviewer", "deployer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["tester"]},
        ])

    label = "Pro (7-10 nodes)" if not goal else f"Pro ({len(nodes)} nodes)"
    description = (
        "Deep mode: analyst → optional design/content/asset prep → one or two builders → optional polisher → reviewer + deployer → tester → debugger"
    )
    return {
        "id": "pro",
        "label": label,
        "description": description,
        "nodes": nodes,
        "node_count_min": 7,
        "node_count_max": 10,
        "profile": profile,
    }


def get_template(template_id: str, goal: str = "") -> Optional[Dict]:
    """Look up a template by ID or difficulty alias."""
    normalized = _DIFFICULTY_ALIAS.get(template_id, template_id)
    if normalized == "pro":
        return _build_pro_template(goal)
    return BUILT_IN_TEMPLATES.get(normalized)


def list_templates() -> List[Dict]:
    """Return summary list of all available templates."""
    return [
        {
            "id": t["id"],
            "label": ("Pro (7-10 nodes)" if t["id"] == "pro" else t["label"]),
            "description": (
                "Deep mode: app-selected 7-10 node chain based on goal complexity"
                if t["id"] == "pro"
                else t.get("description", "")
            ),
            "nodeCount": len(t["nodes"]),
            "nodeCountMin": 7 if t["id"] == "pro" else len(t["nodes"]),
            "nodeCountMax": 10 if t["id"] == "pro" else len(t["nodes"]),
        }
        for t in BUILT_IN_TEMPLATES.values()
    ]


def template_nodes(template_id: str, goal: str = "") -> List[Dict]:
    """Return the node definitions for a template, or empty list if not found."""
    tpl = get_template(template_id, goal=goal)
    return list(tpl["nodes"]) if tpl else []
