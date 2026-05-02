"""
Evermind — Built-in Workflow Templates

Defines reusable node sequences aligned with the Orchestrator's plan decomposition.
Each template maps to a difficulty level (simple/standard/pro) and specifies
which NodeExecutions to auto-create when launching a run.
"""

import re
import logging
from typing import Any, Dict, List, Optional

import task_classifier

_logger = logging.getLogger("evermind.workflow_templates")

# ─────────────────────────────────────────────
# Built-in Templates
# ─────────────────────────────────────────────
BUILT_IN_TEMPLATES: Dict[str, Dict] = {
    "simple": {
        "id": "simple",
        "label": "Simple (2 nodes)",
        # v7.8 (maintainer): tester removed; reviewer (when present)
        # subsumes interaction/runtime-error testing duties.
        "description": "Fast mode: builder → deployer",
        "nodes": [
            {"key": "builder",  "label": "Builder",  "depends_on": []},
            {"key": "deployer", "label": "Deployer", "depends_on": ["builder"]},
        ],
    },
    "standard": {
        "id": "standard",
        "label": "Standard (3 nodes)",
        # v7.8: tester folded into reviewer.
        "description": "Balanced mode: builder → reviewer + deployer",
        "nodes": [
            {"key": "builder",  "label": "Builder",  "depends_on": []},
            {"key": "reviewer", "label": "Reviewer", "depends_on": ["builder"]},
            {"key": "deployer", "label": "Deployer", "depends_on": ["builder"]},
        ],
    },
    "pro": {
        "id": "pro",
        "label": "Pro (8-11 nodes)",
        # v7.8: tester removed; debugger now reads runtime_errors[] from reviewer JSON.
        "description": "Deep mode: planner → analyst → specialist prep → builders → reviewer + deployer → debugger",
        "nodes": [
            {"key": "planner",  "label": "Planner",   "depends_on": []},
            {"key": "analyst",  "label": "Analyst",   "depends_on": ["planner"]},
            {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst"]},
            {"key": "builder2", "label": "Builder 2", "depends_on": ["analyst"]},
            {"key": "merger", "label": "Merger", "depends_on": ["builder1", "builder2"]},
            {"key": "reviewer", "label": "Reviewer",  "depends_on": ["merger"]},
            {"key": "deployer", "label": "Deployer",  "depends_on": ["reviewer"]},
            {"key": "debugger", "label": "Debugger",  "depends_on": ["reviewer", "deployer"]},
        ],
    },
    "ultra": {
        "id": "ultra",
        "label": "Ultra (顶级玩家，3-4h~1day)",
        "description": (
            "Product-grade long-task mode: 4 builder 并行 + 多轮 review "
            "(max 5 reject) + 多文件项目脚手架 + 图片/资源 + 打包部署。"
            "时长预期 3-4 小时到一天。只在 cli_mode.enabled + cli_mode.ultra_mode 同时为 True 时启用。"
        ),
        "nodes": [
            {"key": "planner",  "label": "Planner",   "depends_on": []},
            {"key": "analyst",  "label": "Analyst",   "depends_on": ["planner"]},
            {"key": "uidesign", "label": "UI Designer", "depends_on": ["analyst"]},
            {"key": "scribe",   "label": "Scribe (spec 文档)", "depends_on": ["analyst"]},
            # 4 builder 并行：frontend / backend-ish / assets / tests-or-docs
            {"key": "builder1", "label": "Builder 1 (主页 / Landing)",    "depends_on": ["uidesign", "scribe"]},
            {"key": "builder2", "label": "Builder 2 (分页 / sub-routes)", "depends_on": ["uidesign", "scribe"]},
            {"key": "builder3", "label": "Builder 3 (共享组件/样式)",     "depends_on": ["uidesign", "scribe"]},
            {"key": "builder4", "label": "Builder 4 (交互/资源/脚本)",    "depends_on": ["uidesign", "scribe"]},
            {"key": "merger",   "label": "Merger (4-way)", "depends_on": ["builder1", "builder2", "builder3", "builder4"]},
            {"key": "polisher", "label": "Polisher",  "depends_on": ["merger"]},
            {"key": "reviewer", "label": "Reviewer",  "depends_on": ["polisher"]},
            {"key": "patcher",  "label": "Patcher",   "depends_on": ["reviewer"]},
            {"key": "deployer", "label": "Deployer",  "depends_on": ["reviewer", "patcher"]},
            # v7.8: tester removed; debugger consumes reviewer.runtime_errors[] directly
            {"key": "debugger", "label": "Debugger",  "depends_on": ["reviewer", "deployer"]},
        ],
    },
    "optimize": {
        "id": "optimize",
        "label": "Optimize (4 nodes)",
        "description": "Continuation mode: planner → analyst → builder → reviewer",
        "internal": True,
        "nodes": [
            {
                "key": "planner",
                "label": "Planner",
                "task": "Interpret the user's latest follow-up as an in-place optimization pass. Produce a short execution skeleton that preserves the strongest working artifact areas while prioritizing the requested changes.",
                "depends_on": [],
            },
            {
                "key": "analyst",
                "label": "Analyst",
                "task": "Research only the missing technical or visual gap for this continuation request. Gather implementation-grade references quickly and emit builder/reviewer handoffs for patching the existing artifact instead of restarting.",
                "depends_on": ["planner"],
            },
            {
                "key": "builder",
                "label": "Builder",
                "task": "Patch the current project in place according to the user's latest request. Read the live artifact first, preserve working routes/gameplay/visual identity, and overwrite the existing files instead of doing a clean-slate rewrite.",
                "depends_on": ["planner", "analyst"],
            },
            {
                "key": "reviewer",
                "label": "Reviewer",
                "task": "Verify that the requested follow-up changes landed, no previous strengths regressed, and the updated artifact is ready for the next user review.",
                "depends_on": ["builder"],
            },
        ],
    },
}

# Map difficulty aliases to template IDs (for backward compat)
_DIFFICULTY_ALIAS: Dict[str, str] = {
    "simple": "simple",
    "standard": "standard",
    "pro": "pro",
    "ultra": "ultra",        # v7.1 (maintainer) 顶级玩家长任务模式
    "fast": "simple",
    "balanced": "standard",
    "advanced": "pro",
    "product": "ultra",      # 用户可能说"product mode"
    "ultra_mode": "ultra",
    "long_task": "ultra",
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
    r"(插画|illustration|hero image|lookbook|封面|海报|poster|concept art|concept sheet|turnaround|storyboard|"
    r"素材包|asset pack|概念资产包|概念包|概念图包|角色设定|怪物设定|武器设定|场景设定|render|"
    # v7.3.6 (maintainer): include "建模" / "modeling" / "精灵图" /
    # commercial-grade game cues so 2D PvZ-style games that explicitly
    # request crafted enemy / plant / character art trigger the imagegen +
    # spritesheet + assetimport pipeline. Previously these went on the bare
    # "no-asset" 2D path and the builder had to draw every sprite via raw
    # CSS/canvas, losing visual fidelity.
    r"角色建模|怪物建模|monster\s*model|character\s*model|enemy\s*model|"
    r"sprite\s*sheet|精灵图|游戏素材|game\s*art|character\s*art|game\s*asset|"
    r"商业级游戏|commercial-?grade\s*game|premium\s*game|aaa\s*game|production-?grade\s*game|shippable\s*game|"
    r"建模精致|精致建模|精美建模|精致美术|精美美术|"
    r"建模精细|精细建模|建模精美|精细美术|"
    # v7.7 audit: English equivalents the audit found missing.
    r"detailed\s*modeling|fine\s*modeling|polished\s*modeling|premium\s*modeling|exquisite\s*modeling|"
    r"high[- ]?fidelity\s*(?:art|modeling|game|2d|3d)|professional\s*(?:art|sprites|2d|sprite\s*work)|"
    r"pixel\s*art|polished\s*(?:retro|pixel|2d|game|sprite)|"
    r"commercial-?grade\s*(?:art|game|2d|3d|shooter|rpg|platformer|sprites)|"
    r"polished\s*(?:art|sprites|pixel\s*art)|hi-?fi\s*sprites|hand[- ]?drawn|hand[- ]?painted|"
    r"high[- ]?quality\s*(?:character\s*art|sprites|2d|3d|game|2d\s*assets?)|"
    r"detailed\s*(?:character\s*art|enemy\s*art|monster\s*art)|"
    # PvZ / tower defense / multi-class enemy signals
    r"植物大战僵尸|pvz|plants?\s*vs\.?\s*zombies?|plants?\s*and\s*zombies?|塔防游戏|塔防|"
    r"tower\s*defense|td\s*game|wave\s*defense|"
    r"不同的怪物.*不同的植物|不同的植物.*不同的怪物|多种怪物.*多种植物|"
    r"multiple\s*(?:distinct\s*)?(?:enemies|monsters|plants|towers|characters)\s*and\s*(?:enemies|monsters|plants|towers|characters)|"
    # v7.43 (maintainer): observed in run_4f4e5f0766b0 — user goal
    # "2D版的保卫萝卜手游...怪物的种类...防御塔的种类...升级...关卡...金币
    # 皮肤" got asset_heavy=False because none of these classic Chinese
    # tower-defense terms were in the regex. Result: pipeline picked
    # uidesign+scribe (website nodes) instead of imagegen+spritesheet+
    # assetimport (game asset nodes), so the game had no real sprites.
    r"保卫萝卜|carrot\s*fantasy|carrot\s*defense|"
    r"防御塔|防御.*塔|tower\b|"
    r"怪物.*(?:种类|类型|皮肤|升级)|(?:种类|类型|皮肤|升级).*怪物|"
    r"(?:升级|关卡|金币|皮肤|宝石|装备|武器).*(?:升级|关卡|金币|皮肤|宝石|装备|武器)|"
    r"loot\s*system|level\s*up|skin\s*system|gacha|"
    # 2D platformer / arcade / shooter staples that need sprites
    r"2d\s*platformer|2d\s*shooter|2d\s*arcade|street\s*fighter|fighting\s*game|"
    r"横版游戏|横版动作|动作游戏|射击游戏|fps\b|tps\b|"
    # Roguelike / dungeon / bullet-hell — sprite-heavy
    r"roguelike|rogue\s*lite|dungeon\s*crawler|bullet\s*hell|地下城)",
    re.IGNORECASE,
)
_OPTIMIZE_SMALL_PATCH_RE = re.compile(
    r"(修复|修一下|修正|微调|调整|改一下|小改|优化一下|导航|文案|配色|间距|按钮|图标|布局|"
    r"fix|patch|tweak|polish|refine|adjust|spacing|copy|cta|navbar|button|padding|"
    # v7.7 audit: English equivalents that previously had no match
    r"navigation|nav\b|color\s*scheme|palette|color|layout|icons?|"
    r"change\s*the|update\s*the|move\s*the)",
    re.IGNORECASE,
)
_OPTIMIZE_PARALLEL_RE = re.compile(
    r"(并行|builder1|builder2|merger|合并器|合并期|多个子系统|系统级|重构|大改|全链路|整体架构|"
    r"parallel|merge|merger|multi-system|system-wide|refactor|subsystem|architecture|pipeline)",
    re.IGNORECASE,
)
_OPTIMIZE_VERIFY_RE = re.compile(
    r"(测试|试玩|回归|验收|部署|加载|黑屏|性能|帧率|鼠标|视角|相机|输入|控制|射击|碰撞|卡顿|延迟|"
    r"test|qa|verify|regression|deploy|loading|black screen|performance|fps|frame\s*rate|"
    r"camera|input|controls|shoot|mouse|view|perspective|collision|"
    r"playtest|play\s*through|e2e|end[- ]?to[- ]?end|smoke\s*test|regression\s*test|"
    r"lag|latency|stutter)",
    re.IGNORECASE,
)
_OPTIMIZE_STRATEGIC_DESIGN_RE = re.compile(
    r"(品牌|品牌感|视觉|风格|高级|高端|体验|调性|动画|动效|转场|"
    r"brand|branding|visual|style|premium|motion|animation|transition|experience)",
    re.IGNORECASE,
)


def _prepend_planner_node(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deep-mode runs always start with a visible planner node."""
    if not nodes:
        return [{"key": "planner", "label": "Planner", "depends_on": []}]
    if str(nodes[0].get("key") or "").strip() == "planner":
        return nodes

    adjusted: List[Dict[str, Any]] = [{"key": "planner", "label": "Planner", "depends_on": []}]
    analyst_seen = False
    for node in nodes:
        item = dict(node)
        deps = list(item.get("depends_on") or [])
        if str(item.get("key") or "").strip() == "analyst":
            analyst_seen = True
            item["depends_on"] = ["planner"]
        else:
            item["depends_on"] = deps
        adjusted.append(item)
    if not analyst_seen:
        # Defensive fallback: if a future template accidentally omits analyst,
        # still force the first root work node to wait for planner.
        for index, node in enumerate(adjusted[1:], start=1):
            if not list(node.get("depends_on") or []):
                adjusted[index] = {**node, "depends_on": ["planner"]}
                break
    return adjusted


def _dedupe_depends_on(deps: List[str]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in deps or []:
        dep = str(raw or "").strip()
        if not dep or dep in seen:
            continue
        seen.add(dep)
        ordered.append(dep)
    return ordered


def _enforce_dual_builder_merger(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """If a pro chain has builder1+builder2, force merger before downstream ship gates."""
    normalized: List[Dict[str, Any]] = []
    for node in nodes or []:
        cloned = dict(node or {})
        cloned["depends_on"] = _dedupe_depends_on(list(cloned.get("depends_on") or []))
        normalized.append(cloned)
    keys = [str(node.get("key") or "").strip() for node in normalized]
    if "builder1" not in keys or "builder2" not in keys:
        return normalized

    if "merger" not in keys:
        insert_index = max(keys.index("builder1"), keys.index("builder2")) + 1
        normalized.insert(
            insert_index,
            {"key": "merger", "label": "Merger", "depends_on": ["builder1", "builder2"]},
        )
        keys.insert(insert_index, "merger")
    else:
        merger_index = keys.index("merger")
        merger_node = dict(normalized[merger_index])
        merger_node["depends_on"] = ["builder1", "builder2"]
        normalized[merger_index] = merger_node

    for idx, node in enumerate(normalized):
        key = str(node.get("key") or "").strip()
        deps = _dedupe_depends_on(list(node.get("depends_on") or []))
        has_direct_builder_deps = any(dep in {"builder1", "builder2"} for dep in deps)
        if key == "polisher" and has_direct_builder_deps:
            rewritten = [dep for dep in deps if dep not in {"builder1", "builder2"}]
            if "merger" not in rewritten:
                rewritten.insert(0, "merger")
            node["depends_on"] = _dedupe_depends_on(rewritten)
            normalized[idx] = node
            continue
        if key in {"reviewer", "deployer"} and has_direct_builder_deps:
            rewritten = [dep for dep in deps if dep not in {"builder1", "builder2"}]
            if "merger" not in rewritten:
                rewritten.insert(0, "merger")
            node["depends_on"] = _dedupe_depends_on(rewritten)
            normalized[idx] = node

    return normalized


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

    # ── Diagnostic: log all decision factors for pro template ──
    _logger.info(
        "pro_template_profile: task_type=%s len=%d multi_page=%s visual_complex=%s "
        "content_complex=%s asset_heavy=%s arch_complex=%s long_brief=%s polisher=%s",
        task_type, len(text), multi_page, visual_complex,
        content_complex, asset_heavy, architecture_complex, long_brief, include_polisher,
    )

    if asset_heavy and task_type == "game":
        return {
            "node_count": 12,
            "include_uidesign": False,
            "include_scribe": False,
            "include_asset_pipeline": True,
            "include_polisher": False,
            "parallel_builders": True,
            # v7.1i (maintainer): was 3 (forced game>=3 branch in
            # _parallel_builder_task_descriptions which split builder1=core
            # / builder2=support module → kimi-k2.6 wouldn't obey "no index.html"
            # → retry storm 12+min). NE creation only makes builder1+builder2
            # anyway, so use 2 for consistency.
            "builder_count": 2,
            "reason": "asset_heavy_game_parallel",
        }

    if asset_heavy:
        return {
            "node_count": 12,
            "include_uidesign": False,
            "include_scribe": False,
            "include_asset_pipeline": True,
            "include_polisher": False,
            "parallel_builders": True,
            "builder_count": 2,
            "reason": "asset_heavy",
        }

    if task_type == "game":
        return {
            "node_count": 11,
            "include_uidesign": True,
            "include_scribe": True,
            "include_asset_pipeline": False,
            "include_polisher": False,
            "parallel_builders": True,
            # v7.1i (maintainer): same fix as asset_heavy_game above —
            # was 3, NE only creates 2, kimi unable to obey support-only
            # constraint, retry storm. Standard pro game uses 2 builders.
            "builder_count": 2,
            "scribe_blocks_builders": True,
            "reason": "game_parallel",
        }

    # Games and other architecture_complex types inherently benefit from a
    # uidesign node.  Chinese briefs are far denser than English ones — a 60-char
    # Chinese goal often conveys what takes 180+ English chars — so we apply a
    # lower character threshold for architecture_complex tasks.
    needs_uidesign = multi_page or visual_complex or long_brief or (
        architecture_complex and (content_complex or len(text) >= 40)
    )
    needs_scribe = (
        (multi_page and page_count >= 6)
        or content_complex
        or (architecture_complex and long_brief)
    )
    large_multi_page_website = task_type == "website" and multi_page and page_count >= 6

    if needs_uidesign and needs_scribe:
        # v7.6: was `parallel_builders = large_multi_page_website` only —
        # which forced dashboard / SaaS / complex creative tasks (which all
        # trigger uidesign+scribe) into SEQUENTIAL builders, doubling pro
        # mode wall time. Round 4 PvZ-dashboard sat 28 min on builder1
        # while builder2 was queued. Now: any task with significant
        # architecture complexity (dashboard/presentation/creative) also
        # gets parallel builders. Single-page sites without complex arch
        # remain serial (no benefit from 2 builders on a tiny brief).
        # v7.55 (maintainer): add third trigger — single-page but
        # visually + content rich with a long brief. Observed run
        # 2026-04-30 18:55: "Awwwards-grade UI/UX motion designer + 906
        # char brief, visual_complex=True content_complex=True
        # long_brief=True multi_page=False arch_complex=False" → fell to
        # single builder because neither of the original two triggers
        # matched. A 906-char design-heavy brief deserves 2 builders
        # competing on motion/layout interpretation, then a merger picks
        # the better one. Only fires when ALL THREE flags are set so
        # short single-page tasks stay serial.
        rich_singlepage = (
            visual_complex
            and content_complex
            and long_brief
        )
        parallel_builders = (
            large_multi_page_website
            or (architecture_complex and task_type in {"dashboard", "presentation", "creative", "game"})
            or rich_singlepage
        )
        return {
            "node_count": (
                12 if include_polisher else 11
            ) if parallel_builders else (
                10 if include_polisher else 9
            ),
            "include_uidesign": True,
            "include_scribe": True,
            "include_asset_pipeline": False,
            "include_polisher": include_polisher,
            "parallel_builders": parallel_builders,
            "builder_count": 2 if parallel_builders else 1,
            "scribe_blocks_builders": not parallel_builders,
            "reason": (
                "design_and_content_complexity_parallelized"
                if parallel_builders else
                "design_and_content_complexity"
            ),
        }

    if needs_uidesign:
        return {
            "node_count": 11 if include_polisher else 10,
            "include_uidesign": True,
            "include_scribe": False,
            "include_asset_pipeline": False,
            "include_polisher": include_polisher,
            "parallel_builders": True,
            "builder_count": 2,
            "reason": "design_complexity",
        }

    return {
        "node_count": 10 if include_polisher else 9,
        "include_uidesign": False,
        "include_scribe": False,
        "include_asset_pipeline": False,
        "include_polisher": include_polisher,
        "parallel_builders": True,
        "builder_count": 2,
        "reason": "baseline_polished" if include_polisher else "baseline",
    }


def _build_pro_template(goal: str = "") -> Dict[str, Any]:
    profile = pro_template_profile(goal)
    include_polisher = bool(profile.get("include_polisher"))
    task_type = task_classifier.classify(str(goal or "")).task_type if str(goal or "").strip() else "website"
    parallel_builders = bool(profile.get("parallel_builders", True))
    builder_count = max(1, int(profile.get("builder_count", 2) or 2))
    # v7.1i (maintainer): WAS `task_type != "website" and builder_count <= 2`
    # which forced game/tool/etc 2-builder pro plans into SERIAL mode (builder2
    # depends_on builder1). This contradicted the "real parallelism" design
    # where builder1 writes index.html + builder2 writes game_features.js
    # concurrently and merger SKIP-LLM auto-wires them.
    # Now: only force serial if user explicitly opts out of parallel_builders.
    sequential_game_builders = (not parallel_builders) and task_type != "website" and builder_count <= 2
    if profile["include_asset_pipeline"]:
        if parallel_builders and builder_count >= 3 and task_type != "website":
            nodes = [
                {"key": "analyst", "label": "Analyst", "depends_on": []},
                {"key": "imagegen", "label": "Image Gen", "depends_on": ["analyst"]},
                {"key": "spritesheet", "label": "Spritesheet", "depends_on": ["analyst", "imagegen"]},
                {"key": "assetimport", "label": "Asset Import", "depends_on": ["analyst", "imagegen", "spritesheet"]},
                {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst", "assetimport"]},
                {"key": "builder2", "label": "Builder 2", "depends_on": ["analyst", "assetimport"]},
                {"key": "merger", "label": "Merger", "depends_on": ["builder1", "builder2"]},
                {"key": "reviewer", "label": "Reviewer", "depends_on": ["merger"]},
                {"key": "deployer", "label": "Deployer", "depends_on": ["reviewer"]},
                {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
            ]
        elif parallel_builders:
            if sequential_game_builders:
                nodes = [
                    {"key": "analyst", "label": "Analyst", "depends_on": []},
                    {"key": "imagegen", "label": "Image Gen", "depends_on": ["analyst"]},
                    {"key": "spritesheet", "label": "Spritesheet", "depends_on": ["analyst", "imagegen"]},
                    {"key": "assetimport", "label": "Asset Import", "depends_on": ["analyst", "imagegen", "spritesheet"]},
                    {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst", "assetimport"]},
                    {"key": "builder2", "label": "Builder 2", "depends_on": ["builder1"]},
                    {"key": "reviewer", "label": "Reviewer", "depends_on": ["builder1", "builder2"]},
                    {"key": "deployer", "label": "Deployer", "depends_on": ["reviewer"]},
                    {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
                ]
            else:
                nodes = [
                    {"key": "analyst", "label": "Analyst", "depends_on": []},
                    {"key": "imagegen", "label": "Image Gen", "depends_on": ["analyst"]},
                    {"key": "spritesheet", "label": "Spritesheet", "depends_on": ["analyst", "imagegen"]},
                    {"key": "assetimport", "label": "Asset Import", "depends_on": ["analyst", "imagegen", "spritesheet"]},
                    {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst", "assetimport"]},
                    {"key": "builder2", "label": "Builder 2", "depends_on": ["analyst", "assetimport"]},
                    {"key": "merger", "label": "Merger", "depends_on": ["builder1", "builder2"]},
                    {"key": "reviewer", "label": "Reviewer", "depends_on": ["merger"]},
                    {"key": "deployer", "label": "Deployer", "depends_on": ["reviewer"]},
                    {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
                ]
        else:
            nodes = [
                {"key": "analyst", "label": "Analyst", "depends_on": []},
                {"key": "imagegen", "label": "Image Gen", "depends_on": ["analyst"]},
                {"key": "spritesheet", "label": "Spritesheet", "depends_on": ["analyst", "imagegen"]},
                {"key": "assetimport", "label": "Asset Import", "depends_on": ["analyst", "imagegen", "spritesheet"]},
                {"key": "builder", "label": "Builder", "depends_on": ["analyst", "assetimport"]},
                {"key": "reviewer", "label": "Reviewer", "depends_on": ["builder"]},
                {"key": "deployer", "label": "Deployer", "depends_on": ["builder"]},
                {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
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
            {"key": "deployer", "label": "Deployer", "depends_on": ["reviewer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
        ])
    elif profile["include_uidesign"] and profile["include_scribe"] and builder_count >= 3 and task_type != "website":
        scribe_blocks_builders = bool(profile.get("scribe_blocks_builders", True))
        builder_depends_on = ["analyst", "uidesign"]
        if scribe_blocks_builders:
            builder_depends_on.append("scribe")
        nodes = [
            {"key": "analyst", "label": "Analyst", "depends_on": []},
            {"key": "uidesign", "label": "UI Design", "depends_on": ["analyst"]},
            {"key": "scribe", "label": "Scribe", "depends_on": ["analyst"]},
            {"key": "builder1", "label": "Builder 1", "depends_on": builder_depends_on},
            {"key": "builder2", "label": "Builder 2", "depends_on": builder_depends_on},
            {"key": "merger", "label": "Merger", "depends_on": ["builder1", "builder2"]},
        ]
        if include_polisher:
            nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["merger"]})
            polish_dep = ["polisher"]
        else:
            polish_dep = ["merger"]
        nodes.extend([
            {"key": "reviewer", "label": "Reviewer", "depends_on": polish_dep},
            {"key": "deployer", "label": "Deployer", "depends_on": ["reviewer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
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
        if not sequential_game_builders:
            nodes.append({"key": "merger", "label": "Merger", "depends_on": ["builder1", "builder2"]})
            if include_polisher:
                polisher_depends_on = ["merger"]
                if not scribe_blocks_builders:
                    polisher_depends_on.append("scribe")
                nodes.append({"key": "polisher", "label": "Polisher", "depends_on": polisher_depends_on})
                polish_dep = ["polisher"]
            else:
                polish_dep = ["merger"]
        elif include_polisher:
            polisher_depends_on = ["builder1", "builder2"]
            if not scribe_blocks_builders:
                polisher_depends_on.append("scribe")
            nodes.append({"key": "polisher", "label": "Polisher", "depends_on": polisher_depends_on})
            polish_dep = ["polisher"]
        else:
            polish_dep = ["builder1", "builder2"]
        nodes.extend([
            {"key": "reviewer", "label": "Reviewer", "depends_on": polish_dep},
            {"key": "deployer", "label": "Deployer", "depends_on": ["reviewer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
        ])
    elif profile["include_uidesign"]:
        if parallel_builders:
            if sequential_game_builders:
                nodes = [
                    {"key": "analyst", "label": "Analyst", "depends_on": []},
                    {"key": "uidesign", "label": "UI Design", "depends_on": ["analyst"]},
                    {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst", "uidesign"]},
                    {"key": "builder2", "label": "Builder 2", "depends_on": ["builder1"]},
                ]
                if include_polisher:
                    nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["builder1", "builder2"]})
                    polish_dep = ["polisher"]
                else:
                    polish_dep = ["builder1", "builder2"]
            else:
                nodes = [
                    {"key": "analyst", "label": "Analyst", "depends_on": []},
                    {"key": "uidesign", "label": "UI Design", "depends_on": ["analyst"]},
                    {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst", "uidesign"]},
                    {"key": "builder2", "label": "Builder 2", "depends_on": ["analyst", "uidesign"]},
                    {"key": "merger", "label": "Merger", "depends_on": ["builder1", "builder2"]},
                ]
                if include_polisher:
                    nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["merger"]})
                    polish_dep = ["polisher"]
                else:
                    polish_dep = ["merger"]
        else:
            nodes = [
                {"key": "analyst", "label": "Analyst", "depends_on": []},
                {"key": "uidesign", "label": "UI Design", "depends_on": ["analyst"]},
                {"key": "builder", "label": "Builder", "depends_on": ["analyst", "uidesign"]},
            ]
            if include_polisher:
                nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["builder"]})
                polish_dep = ["polisher"]
            else:
                polish_dep = ["builder"]
        nodes.extend([
            {"key": "reviewer", "label": "Reviewer", "depends_on": polish_dep},
            {"key": "deployer", "label": "Deployer", "depends_on": ["reviewer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
        ])
    else:
        if parallel_builders:
            if sequential_game_builders:
                nodes = [
                    {"key": "analyst", "label": "Analyst", "depends_on": []},
                    {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst"]},
                    {"key": "builder2", "label": "Builder 2", "depends_on": ["builder1"]},
                ]
                if include_polisher:
                    nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["builder1", "builder2"]})
                    polish_dep = ["polisher"]
                else:
                    polish_dep = ["builder1", "builder2"]
            else:
                nodes = [
                    {"key": "analyst", "label": "Analyst", "depends_on": []},
                    {"key": "builder1", "label": "Builder 1", "depends_on": ["analyst"]},
                    {"key": "builder2", "label": "Builder 2", "depends_on": ["analyst"]},
                    {"key": "merger", "label": "Merger", "depends_on": ["builder1", "builder2"]},
                ]
                if include_polisher:
                    nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["merger"]})
                    polish_dep = ["polisher"]
                else:
                    polish_dep = ["merger"]
        else:
            nodes = [
                {"key": "analyst", "label": "Analyst", "depends_on": []},
                {"key": "builder", "label": "Builder", "depends_on": ["analyst"]},
            ]
            if include_polisher:
                nodes.append({"key": "polisher", "label": "Polisher", "depends_on": ["builder"]})
                polish_dep = ["polisher"]
            else:
                polish_dep = ["builder"]
        nodes.extend([
            {"key": "reviewer", "label": "Reviewer", "depends_on": polish_dep},
            {"key": "deployer", "label": "Deployer", "depends_on": ["reviewer"]},
            {"key": "debugger", "label": "Debugger", "depends_on": ["reviewer", "deployer"]},
        ])

    nodes = _enforce_dual_builder_merger(nodes)
    # v6.3 (maintainer): every pro branch gets a conditional patcher
    # node that sits after reviewer. It stays dormant on reviewer APPROVE
    # (orchestrator._execute_subtask_inner short-circuits it via the
    # `_reviewer_requeues==0` gate) and only fires on reviewer REJECT with
    # ≤6 localizable issues (orchestrator._patcher_can_handle_reviewer_issues).
    # This previously lived only in orchestrator SubTask construction, so the
    # canvas UI that reads `nodes` here never rendered it — user saw the
    # pipeline run without a visible patcher even though the executor was
    # ready for one. Adding it here keeps UI preview + actual execution in
    # sync without altering downstream dependencies.
    _has_reviewer = any(n.get("key") == "reviewer" for n in nodes)
    _has_patcher = any(n.get("key") == "patcher" for n in nodes)
    if _has_reviewer and not _has_patcher:
        # v6.4 (maintainer): patcher is a canvas-parallel sibling of
        # reviewer (both depend on the last build/polish node) so the UI
        # shows them side-by-side. Runtime execution order is still
        # reviewer → patcher (enforced in orchestrator SubTask construction:
        # patcher.depends_on=[reviewer_id]). This "visual parallel, runtime
        # serial" split matches the user's mental model — patcher is a
        # conditional repair branch, not a post-reviewer downstream.
        _reviewer_node = next((n for n in nodes if n.get("key") == "reviewer"), {})
        _patcher_deps = list(_reviewer_node.get("depends_on") or ["reviewer"])
        nodes.append({
            "key": "patcher",
            "label": "补丁师",
            "depends_on": _patcher_deps,
        })

    _logger.info(
        "_build_pro_template: node_count=%d reason=%s node_keys=[%s]",
        len(nodes),
        profile.get("reason", "?"),
        ", ".join(n["key"] for n in nodes),
    )

    nodes = _prepend_planner_node(nodes)

    label = "Pro (10-13 nodes)" if not goal else f"Pro ({len(nodes)} nodes)"
    description = (
        "Deep mode: planner → analyst → optional design/content/asset prep → one to three builders → optional polisher → reviewer + deployer → debugger → patcher (conditional). v7.8: tester folded into reviewer."
    )
    return {
        "id": "pro",
        "label": label,
        "description": description,
        "nodes": nodes,
        "node_count_min": 10,
        "node_count_max": 13,
        "profile": profile,
    }


def optimize_template_profile(goal: str = "") -> Dict[str, Any]:
    """Return a continuation/optimization template profile for the latest goal."""
    text = str(goal or "").strip()
    lowered = text.lower()
    task_type = task_classifier.classify(text).task_type if text else "website"
    multi_page = task_classifier.wants_multi_page(text) if text else False
    asset_heavy = bool(_PRO_ASSET_HEAVY_RE.search(text)) or task_classifier.wants_generated_assets(text)
    long_brief = len(text) >= 140
    parallel_hint = bool(_OPTIMIZE_PARALLEL_RE.search(text))
    verify_hint = bool(_OPTIMIZE_VERIFY_RE.search(text))
    small_patch = (
        bool(_OPTIMIZE_SMALL_PATCH_RE.search(text))
        and len(text) <= 120
        and not multi_page
        and task_type not in {"game", "dashboard", "tool"}
        and not parallel_hint
        and not asset_heavy
        and not _OPTIMIZE_STRATEGIC_DESIGN_RE.search(text)
    )
    game_heavy = task_type == "game" and (parallel_hint or verify_hint or asset_heavy or long_brief or len(text) >= 60)
    complex_parallel = bool(
        parallel_hint
        or asset_heavy
        or game_heavy
        or (multi_page and long_brief)
        or ("performance" in lowered and task_type in {"dashboard", "tool"})
    )
    needs_tester = False  # v7.8: tester removed; reviewer subsumes test duties

    if small_patch:
        return {
            "mode": "patch_fast",
            "parallel_builders": False,
            "needs_planner": False,
            "needs_analyst": False,
            "needs_tester": False,
            "reason": "small_patch",
        }
    if complex_parallel:
        return {
            "mode": "parallel_optimize",
            "parallel_builders": True,
            "needs_planner": True,
            "needs_analyst": True,
            "needs_tester": needs_tester,
            "reason": "parallel_game_or_complex_continuation" if task_type == "game" else "parallel_complex_continuation",
        }
    return {
        "mode": "standard_optimize",
        "parallel_builders": False,
        "needs_planner": True,
        "needs_analyst": True,
        "needs_tester": needs_tester,
        "reason": "standard_continuation",
    }


def _build_optimize_template(goal: str = "") -> Dict[str, Any]:
    text = str(goal or "").strip()
    task_type = task_classifier.classify(text).task_type if text else "website"
    profile = optimize_template_profile(text)
    needs_tester = bool(profile.get("needs_tester"))

    if profile["mode"] == "patch_fast":
        nodes: List[Dict[str, Any]] = [
            {
                "key": "builder",
                "label": "Builder",
                "task": "Patch the current project in place for this targeted follow-up. Read the live files first, keep the strongest existing implementation, and make the smallest high-confidence edits that solve the request.",
                "depends_on": [],
            },
            {
                "key": "reviewer",
                "label": "Reviewer",
                "task": "Verify the requested patch landed without regression and produce a concrete patch-oriented rollback report if anything still fails.",
                "depends_on": ["builder"],
            },
        ]
    elif profile["mode"] == "parallel_optimize":
        if task_type == "game":
            builder1_task = (
                "Patch gameplay-critical systems in the current game: input mapping, camera feel, aiming, firing, hit feedback, and other moment-to-moment controls. "
                "Start from the existing playable files and edit them in place."
            )
            builder2_task = (
                "Patch integration-critical systems in the current game: HUD, enemy behavior, progression, level flow, feedback readability, and performance-sensitive glue code. "
                "Preserve working mechanics and edit the existing files in place."
            )
        else:
            builder1_task = (
                "Patch the current project's structural layer in place: layout system, routes, reusable components, navigation, and content architecture. "
                "Keep the existing project identity and strongest working sections."
            )
            builder2_task = (
                "Patch the current project's experience layer in place: visuals, animation/polish, responsiveness, state handling, and user-facing quality gaps. "
                "Keep the existing project identity and strongest working sections."
            )
        nodes = [
            {
                "key": "planner",
                "label": "Planner",
                "task": "Interpret the latest follow-up as a structured in-place optimization pass. Split the work so parallel builders have distinct ownership and the merger receives a conflict-aware integration plan.",
                "depends_on": [],
            },
            {
                "key": "analyst",
                "label": "Analyst",
                "task": "Research only the missing technical gaps for this continuation request. Gather implementation-grade references fast, highlight regression risks, and emit patch-first handoffs for the builders and merger.",
                "depends_on": ["planner"],
            },
            {"key": "builder1", "label": "Builder 1", "task": builder1_task, "depends_on": ["planner", "analyst"]},
            {"key": "builder2", "label": "Builder 2", "task": builder2_task, "depends_on": ["planner", "analyst"]},
            {
                "key": "merger",
                "label": "Merger",
                "task": "Merge the parallel builder outputs into one coherent live project. Re-read the current artifact first, keep the best implementation from each lane, resolve conflicts surgically, and produce one integrated deliverable without regressing prior strengths.",
                "depends_on": ["builder1", "builder2"],
            },
            {
                "key": "reviewer",
                "label": "Reviewer",
                "task": "Review the merged artifact against the latest request and the inherited project strengths. If rejecting, produce a detailed human-readable rollback report that tells the next node exactly what to patch instead of rewriting from scratch.",
                "depends_on": ["merger"],
            },
        ]
        if needs_tester:
            nodes.append({
                "key": "tester",
                "label": "Tester",
                "task": "Run an interaction-focused regression pass on the optimized artifact. Prove the requested fixes landed and no loading, black-screen, input, or gameplay regression was introduced.",
                "depends_on": ["reviewer"],
            })
    else:
        nodes = [
            {
                "key": "planner",
                "label": "Planner",
                "task": "Interpret the user's latest follow-up as an in-place optimization pass. Produce a short execution skeleton that preserves the strongest working artifact areas while prioritizing the requested changes.",
                "depends_on": [],
            },
            {
                "key": "analyst",
                "label": "Analyst",
                "task": "Research only the missing technical or visual gap for this continuation request. Gather implementation-grade references quickly and emit builder/reviewer handoffs for patching the existing artifact instead of restarting.",
                "depends_on": ["planner"],
            },
            {
                "key": "builder",
                "label": "Builder",
                "task": "Patch the current project in place according to the user's latest request. Read the live artifact first, preserve working routes/gameplay/visual identity, and overwrite the existing files instead of doing a clean-slate rewrite.",
                "depends_on": ["planner", "analyst"],
            },
            {
                "key": "reviewer",
                "label": "Reviewer",
                "task": "Verify that the requested follow-up changes landed, no previous strengths regressed, and the updated artifact is ready for the next user review.",
                "depends_on": ["builder"],
            },
        ]
        if needs_tester:
            nodes.append({
                "key": "tester",
                "label": "Tester",
                "task": "Exercise the updated artifact and check for interaction, rendering, and loading regressions before the next user review.",
                "depends_on": ["reviewer"],
            })

    return {
        "id": "optimize",
        "label": f"Optimize ({len(nodes)} nodes)",
        "description": "Continuation mode: app-selected patch / optimize / merge flow based on the latest follow-up complexity",
        "internal": True,
        "nodes": nodes,
        "profile": profile,
    }


def _build_ultra_template_for_goal(goal: str) -> Dict:
    """v7.1i (maintainer): adapt the 4-builder labels to task type.
    Hardcoded "主页 / 分页 / 共享组件 / 交互资源" labels confused the LLM
    when goal was a 3D shooter game (or any non-website project) — Builder 1
    saw "主页 / Landing" and the prompt header said "3D shooting game"
    in the same call. Now we pick lane labels by goal classification.
    """
    base = BUILT_IN_TEMPLATES.get("ultra")
    if not base:
        return base or {}
    profile = task_classifier.classify(goal or "")
    task_type = profile.task_type
    # Lane labels per task type. Each is a (label_zh, label_en) tuple style
    # baked into one string. The LLM-facing "owns this lane" prompt comes
    # from orchestrator._builder_focus_map(); these labels are mainly for
    # UI display and prompt header context.
    LANE_LABELS = {
        "game": [
            "Builder 1 (引擎/渲染/相机)",
            "Builder 2 (玩家控制/输入)",
            "Builder 3 (敌人 AI/武器/物理)",
            "Builder 4 (HUD/关卡/资源)",
        ],
        "tool": [
            "Builder 1 (核心交互逻辑)",
            "Builder 2 (UI/页面)",
            "Builder 3 (数据/状态/持久化)",
            "Builder 4 (集成/打磨)",
        ],
        "creative": [
            "Builder 1 (主体作品)",
            "Builder 2 (变体/章节)",
            "Builder 3 (装饰/动效)",
            "Builder 4 (集成/打磨)",
        ],
        # default = website (existing labels)
        "website": [
            "Builder 1 (主页 / Landing)",
            "Builder 2 (分页 / sub-routes)",
            "Builder 3 (共享组件/样式)",
            "Builder 4 (交互/资源/脚本)",
        ],
    }
    labels = LANE_LABELS.get(task_type) or LANE_LABELS["website"]
    # Deep-copy nodes and replace builder1-4 labels.
    new_nodes: List[Dict[str, Any]] = []
    for n in base["nodes"]:
        nn = dict(n)
        key = nn.get("key", "")
        if key in ("builder1", "builder2", "builder3", "builder4"):
            idx = int(key[-1]) - 1
            if 0 <= idx < len(labels):
                nn["label"] = labels[idx]
        new_nodes.append(nn)
    return {**base, "nodes": new_nodes}


def get_template(template_id: str, goal: str = "") -> Optional[Dict]:
    """Look up a template by ID or difficulty alias."""
    normalized = _DIFFICULTY_ALIAS.get(template_id, template_id)
    if normalized == "pro":
        return _build_pro_template(goal)
    if normalized == "optimize":
        return _build_optimize_template(goal)
    if normalized == "ultra":
        return _build_ultra_template_for_goal(goal)
    return BUILT_IN_TEMPLATES.get(normalized)


def list_templates() -> List[Dict]:
    """Return summary list of all available templates."""
    return [
        {
            "id": t["id"],
            "label": ("Pro (9-12 nodes)" if t["id"] == "pro" else t["label"]),
            "description": (
                "Deep mode: app-selected 9-12 node chain based on goal complexity"
                if t["id"] == "pro"
                else t.get("description", "")
            ),
            "nodeCount": len(t["nodes"]),
            "nodeCountMin": 9 if t["id"] == "pro" else len(t["nodes"]),
            "nodeCountMax": 12 if t["id"] == "pro" else len(t["nodes"]),
        }
        for t in BUILT_IN_TEMPLATES.values()
        if not t.get("internal")
    ]


def template_nodes(template_id: str, goal: str = "") -> List[Dict]:
    """Return the node definitions for a template, or empty list if not found."""
    tpl = get_template(template_id, goal=goal)
    return list(tpl["nodes"]) if tpl else []
