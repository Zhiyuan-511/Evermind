"""
Evermind Agent Skills

Built-in prompt skills live under backend/agent_skills/<skill-name>/SKILL.md.
User-installed community skills live under ~/.evermind/skills/<skill-name>/SKILL.md.
"""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from node_roles import normalize_node_role
import task_classifier

SKILLS_DIR = Path(__file__).parent / "agent_skills"
USER_SKILLS_DIR = Path.home() / ".evermind" / "skills"
COMMUNITY_META_FILE = "evermind_skill.json"
GITHUB_FILE_SIZE_LIMIT = 512 * 1024
GITHUB_TOTAL_SIZE_LIMIT = 3 * 1024 * 1024
ALLOWED_INSTALL_EXTENSIONS = {
    "",
    ".css",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".md",
    ".png",
    ".py",
    ".sh",
    ".svg",
    ".ts",
    ".tsx",
    ".txt",
    ".webp",
    ".yaml",
    ".yml",
}
HTTP_HEADERS = {
    "User-Agent": "Evermind/2.1",
    "Accept": "application/vnd.github+json",
}


SKILL_MAP = {
    "builder": {
        "*": ["commercial-ui-polish", "ui-polish-microstates", "think-act-verify", "structured-handoff", "error-recovery"],
        "website": [
            "commercial-ui-polish",
            "conversion-surface-architecture",
            "motion-choreography-system",
            "evermind-atlas-surface-system",
            "evermind-editorial-layout-composer",
            "evermind-resilient-media-delivery",
            "premium-typography-system",
            "cinematic-visual-narrative",
            "responsive-grid-mastery",
            "immersive-scroll-interactions",
        ],
        "dashboard": ["dashboard-signal-clarity", "data-storytelling-panels", "ui-polish-microstates"],
        "game": ["gameplay-foundation", "game-feel-feedback", "godogen-playable-loop"],
        "tool": ["ui-polish-microstates", "docs-clarity-architecture"],
        "presentation": ["slides-story-arc", "diagram-driven-explainer", "motion-choreography-system", "pptx-export-bridge"],
        "creative": ["motion-choreography-system", "svg-illustration-system", "remotion-scene-composer", "ltx-cinematic-video-blueprint"],
    },
    "scribe": {
        "*": ["docs-clarity-architecture"],
        "presentation": ["slides-story-arc", "diagram-driven-explainer", "docs-clarity-architecture", "pptx-export-bridge"],
        "tool": ["docs-clarity-architecture", "diagram-driven-explainer"],
        "dashboard": ["docs-clarity-architecture", "data-storytelling-panels"],
        "creative": ["docs-clarity-architecture", "remotion-scene-composer", "ltx-cinematic-video-blueprint"],
    },
    "uidesign": {
        "*": ["commercial-ui-polish", "design-system-consistency"],
        "website": [
            "commercial-ui-polish",
            "design-system-consistency",
        ],
        "dashboard": ["dashboard-signal-clarity", "data-storytelling-panels", "design-system-consistency"],
        "presentation": ["slides-story-arc", "motion-choreography-system"],
        "creative": ["motion-choreography-system", "svg-illustration-system", "remotion-scene-composer"],
    },
    "imagegen": {
        "*": ["image-prompt-director", "visual-storyboard-shotlist", "comfyui-pipeline-brief"],
        "website": ["image-prompt-director", "svg-illustration-system"],
        "game": ["image-prompt-director", "asset-pipeline-packaging", "visual-storyboard-shotlist"],
        "presentation": ["image-prompt-director", "visual-storyboard-shotlist"],
        "creative": ["image-prompt-director", "visual-storyboard-shotlist", "ltx-cinematic-video-blueprint"],
    },
    "spritesheet": {
        "*": ["pixel-asset-pipeline", "asset-pipeline-packaging"],
        "game": ["pixel-asset-pipeline", "asset-pipeline-packaging", "godogen-playable-loop"],
    },
    "assetimport": {
        "*": ["asset-pipeline-packaging"],
        "game": ["asset-pipeline-packaging", "pixel-asset-pipeline"],
    },
    "reviewer": {
        "*": ["browser-observe-act-verify", "scroll-evidence-capture", "evidence-driven-qa", "design-system-consistency", "confidence-escalation", "decision-audit"],
        "website": [
            "browser-observe-act-verify",
            "scroll-evidence-capture",
            "evidence-driven-qa",
            "design-system-consistency",
            "evermind-resilient-media-delivery",
            "evermind-review-remediation-gate",
        ],
        "dashboard": ["dashboard-signal-clarity", "scroll-evidence-capture", "evidence-driven-qa", "design-system-consistency"],
        "game": ["gameplay-qa-gate", "scroll-evidence-capture", "evidence-driven-qa", "review-escalation-computer-use", "godogen-playable-loop"],
        "tool": ["browser-observe-act-verify", "scroll-evidence-capture", "evidence-driven-qa", "docs-clarity-architecture"],
        "presentation": ["browser-observe-act-verify", "scroll-evidence-capture", "evidence-driven-qa", "slides-story-arc"],
        "creative": ["browser-observe-act-verify", "scroll-evidence-capture", "evidence-driven-qa", "motion-choreography-system", "remotion-scene-composer"],
    },
    "tester": {
        "*": ["browser-observe-act-verify", "scroll-evidence-capture", "evidence-driven-qa", "design-system-consistency", "confidence-escalation"],
        "website": [
            "browser-observe-act-verify",
            "scroll-evidence-capture",
            "evidence-driven-qa",
            "design-system-consistency",
            "evermind-resilient-media-delivery",
            "evermind-review-remediation-gate",
        ],
        "dashboard": ["dashboard-signal-clarity", "scroll-evidence-capture", "evidence-driven-qa", "design-system-consistency"],
        "game": ["gameplay-qa-gate", "scroll-evidence-capture", "evidence-driven-qa", "review-escalation-computer-use", "godogen-playable-loop"],
        "tool": ["browser-observe-act-verify", "scroll-evidence-capture", "evidence-driven-qa", "docs-clarity-architecture"],
        "presentation": ["browser-observe-act-verify", "scroll-evidence-capture", "evidence-driven-qa", "slides-story-arc"],
        "creative": ["browser-observe-act-verify", "scroll-evidence-capture", "evidence-driven-qa", "motion-choreography-system", "remotion-scene-composer"],
    },
    "analyst": {
        "*": ["research-pattern-extraction", "source-first-research-loop", "exhaustive-analysis", "decision-audit"],
        "website": [
            "research-pattern-extraction",
            "source-first-research-loop",
            "conversion-surface-architecture",
            "cinematic-visual-narrative",
            "evermind-atlas-surface-system",
            "evermind-editorial-layout-composer",
            "evermind-resilient-media-delivery",
        ],
        "dashboard": ["research-pattern-extraction", "source-first-research-loop", "data-storytelling-panels"],
        "presentation": ["research-pattern-extraction", "source-first-research-loop", "slides-story-arc"],
        "game": ["research-pattern-extraction", "source-first-research-loop", "pixel-asset-pipeline", "godogen-playable-loop"],
        "creative": ["research-pattern-extraction", "source-first-research-loop", "remotion-scene-composer", "ltx-cinematic-video-blueprint"],
    },
    "polisher": {
        "*": ["commercial-ui-polish", "ui-polish-microstates", "visual-slot-recovery", "scroll-evidence-capture"],
        "website": [
            "commercial-ui-polish",
            "conversion-surface-architecture",
            "motion-choreography-system",
            "ui-polish-microstates",
            "visual-slot-recovery",
            "scroll-evidence-capture",
            "evermind-atlas-surface-system",
            "evermind-editorial-layout-composer",
            "evermind-resilient-media-delivery",
            "premium-typography-system",
            "immersive-scroll-interactions",
        ],
        "dashboard": ["dashboard-signal-clarity", "ui-polish-microstates", "scroll-evidence-capture"],
        "presentation": ["slides-story-arc", "motion-choreography-system", "ui-polish-microstates", "scroll-evidence-capture"],
        "creative": ["motion-choreography-system", "svg-illustration-system", "ui-polish-microstates", "scroll-evidence-capture"],
    },
    "debugger": {
        "*": ["think-act-verify", "error-recovery", "exhaustive-analysis", "decision-audit"],
    },
    "deployer": {
        "*": ["structured-handoff"],
    },
    "merger": {
        "*": ["think-act-verify", "structured-handoff", "error-recovery", "decision-audit"],
    },
    "planner": {
        "*": ["exhaustive-analysis", "decision-audit", "confidence-escalation"],
    },
}


# ── Task-type exclusion: prevent cross-domain skill contamination ──
# When task_type is "website", game-specific skills should NOT be loaded,
# and vice versa. This prevents agents from misusing irrelevant plugins.
SKILL_EXCLUSION_MAP: Dict[str, set] = {
    "website": {
        "gameplay-foundation", "game-feel-feedback", "godogen-playable-loop",
        "pixel-asset-pipeline", "asset-pipeline-packaging", "gameplay-qa-gate",
    },
    "game": {
        "conversion-surface-architecture", "commercial-ui-polish",
        "slides-story-arc", "pptx-export-bridge", "diagram-driven-explainer",
    },
    "presentation": {
        "gameplay-foundation", "game-feel-feedback", "godogen-playable-loop",
        "pixel-asset-pipeline", "asset-pipeline-packaging", "gameplay-qa-gate",
    },
    "dashboard": {
        "gameplay-foundation", "game-feel-feedback", "godogen-playable-loop",
        "pixel-asset-pipeline", "asset-pipeline-packaging", "gameplay-qa-gate",
        "slides-story-arc", "pptx-export-bridge",
    },
    "tool": {
        "gameplay-foundation", "game-feel-feedback", "godogen-playable-loop",
        "pixel-asset-pipeline", "asset-pipeline-packaging", "gameplay-qa-gate",
        "slides-story-arc", "pptx-export-bridge",
    },
    "creative": {
        "gameplay-foundation", "game-feel-feedback", "godogen-playable-loop",
        "pixel-asset-pipeline", "asset-pipeline-packaging", "gameplay-qa-gate",
        "conversion-surface-architecture", "dashboard-signal-clarity",
    },
}


GOAL_SKILL_RULES = [
    {
        "pattern": re.compile(r"(PPT|slides|幻灯片|演示|presentation|keynote|pitch deck|汇报)", re.IGNORECASE),
        "skills": {
            "builder": ["slides-story-arc", "diagram-driven-explainer", "pptx-export-bridge"],
            "scribe": ["slides-story-arc", "diagram-driven-explainer", "pptx-export-bridge"],
            "reviewer": ["slides-story-arc"],
            "tester": ["slides-story-arc"],
            "analyst": ["slides-story-arc"],
            "uidesign": ["slides-story-arc"],
        },
    },
    {
        "pattern": re.compile(r"(文档|documentation|docs|README|手册|guide|spec|白皮书|api doc|技术文档|教程)", re.IGNORECASE),
        "skills": {
            "builder": ["docs-clarity-architecture"],
            "scribe": ["docs-clarity-architecture", "diagram-driven-explainer"],
            "reviewer": ["docs-clarity-architecture"],
            "tester": ["docs-clarity-architecture"],
            "analyst": ["docs-clarity-architecture"],
        },
    },
    {
        "pattern": re.compile(r"(图像|图片|海报|封面|插画|illustration|poster|concept art|角色设定|生成图片|image)", re.IGNORECASE),
        "skills": {
            "builder": ["svg-illustration-system", "image-prompt-director"],
            "imagegen": ["image-prompt-director", "visual-storyboard-shotlist", "comfyui-pipeline-brief"],
            "uidesign": ["svg-illustration-system", "image-prompt-director"],
            "analyst": ["image-prompt-director", "visual-storyboard-shotlist"],
            "scribe": ["image-prompt-director"],
        },
    },
    {
        "pattern": re.compile(r"(动画|motion|动效|Lottie|loading|transition|micro.?interaction|hover animation|滚动动画)", re.IGNORECASE),
        "skills": {
            "builder": ["motion-choreography-system", "lottie-readiness"],
            "polisher": ["motion-choreography-system", "lottie-readiness"],
            "uidesign": ["motion-choreography-system", "lottie-readiness"],
            "reviewer": ["motion-choreography-system"],
            "tester": ["motion-choreography-system"],
            "analyst": ["motion-choreography-system"],
        },
    },
    {
        "pattern": re.compile(r"(游戏素材|game asset|sprite|spritesheet|像素|pixel art|tileset|角色素材|特效素材)", re.IGNORECASE),
        "skills": {
            "builder": ["pixel-asset-pipeline", "image-prompt-director"],
            "imagegen": ["pixel-asset-pipeline", "visual-storyboard-shotlist"],
            "spritesheet": ["pixel-asset-pipeline", "asset-pipeline-packaging"],
            "assetimport": ["asset-pipeline-packaging", "pixel-asset-pipeline"],
            "analyst": ["pixel-asset-pipeline"],
        },
    },
    {
        "pattern": re.compile(r"(品牌|brand|logo|icon|吉祥物|mascot|视觉识别|identity)", re.IGNORECASE),
        "skills": {
            "builder": ["svg-illustration-system", "conversion-surface-architecture"],
            "polisher": ["svg-illustration-system", "conversion-surface-architecture"],
            "uidesign": ["svg-illustration-system", "design-system-consistency"],
            "imagegen": ["image-prompt-director", "visual-storyboard-shotlist"],
            "analyst": ["visual-storyboard-shotlist"],
        },
    },
    {
        "pattern": re.compile(r"(视频|video|短片|宣传片|trailer|promo|片头|storyboard|镜头脚本|分镜|动画短片|短视频|reel)", re.IGNORECASE),
        "skills": {
            "builder": ["remotion-scene-composer", "ltx-cinematic-video-blueprint"],
            "scribe": ["remotion-scene-composer", "ltx-cinematic-video-blueprint"],
            "uidesign": ["remotion-scene-composer"],
            "imagegen": ["ltx-cinematic-video-blueprint", "visual-storyboard-shotlist"],
            "reviewer": ["remotion-scene-composer"],
            "tester": ["remotion-scene-composer"],
            "analyst": ["remotion-scene-composer", "ltx-cinematic-video-blueprint"],
        },
    },
    {
        "pattern": re.compile(
            r"(?=.*(?:游戏|game|shooter|射击|战斗|怪物|weapon|枪械))(?=.*(?:3d|建模|模型|modeling|rig|skeleton|third.?person|first.?person|tps|fps))",
            re.IGNORECASE,
        ),
        "skills": {
            "builder": ["godogen-visual-target-lock", "godogen-3d-asset-replacement"],
            "imagegen": ["godogen-visual-target-lock", "godogen-3d-asset-replacement"],
            "spritesheet": ["godogen-3d-asset-replacement"],
            "assetimport": ["godogen-3d-asset-replacement"],
            "analyst": ["godogen-visual-target-lock", "godogen-3d-asset-replacement"],
        },
    },
    {
        # v6.1.5 (Opus A R3): broaden activation to catch ANY 3D game with
        # camera/input needs — not just TPS shooters. Prior narrower regex
        # missed flight sims, MMO isometric, first-person puzzle, etc. Now
        # fires when EITHER axis matches: camera-related OR input-related.
        "pattern": re.compile(
            r"(?:"
            # Path A: 3D + any camera/input concept
            r"(?=.*\b(?:3d|three\.?js|webgl|第三人称|third.?person|"
            r"first.?person|\bfps\b|\btps\b|camera.?relative|镜头|视角|"
            r"isometric|等距|top.?down|俯视))"
            r"(?=.*(?:camera|yaw|pitch|mouse|pointer|drag|拖动|wasd|input|"
            r"keyboard|gamepad|controls?|控制|输入|fire|shoot|射击|aim|瞄准))"
            r"|"
            # Path B: explicit shooter/combat keywords
            r"(?=.*(?:射击|shooter|\bfps\b|\btps\b|枪|weapon|武器|gunplay))"
            r"(?=.*(?:controls?|wasd|input|控制|输入|keyboard|mouse|手柄))"
            r")",
            re.IGNORECASE,
        ),
        "skills": {
            "builder": ["godogen-tps-control-sanity-lock"],
            "reviewer": ["godogen-tps-control-sanity-lock"],
            "tester": ["godogen-tps-control-sanity-lock"],
            "analyst": ["godogen-tps-control-sanity-lock"],
            "debugger": ["godogen-tps-control-sanity-lock"],
        },
    },
    {
        "pattern": re.compile(r"(godot|可玩原型|playable game|vertical slice|玩法循环|关卡原型|boss 战|platformer|roguelike|top.?down shooter)", re.IGNORECASE),
        "skills": {
            "builder": ["godogen-playable-loop"],
            "reviewer": ["godogen-playable-loop"],
            "tester": ["godogen-playable-loop"],
            "analyst": ["godogen-playable-loop"],
            "spritesheet": ["godogen-playable-loop"],
        },
    },
]


SKILL_LIBRARY_HINTS: Dict[str, Dict[str, Any]] = {
    "commercial-ui-polish": {
        "title": "Commercial UI Polish",
        "summary": "提升信息层级、留白、CTA 和视觉完成度，让网站和产品页更接近可售卖成品。",
        "category": "ui",
        "tags": ["website", "conversion", "polish"],
    },
    "dashboard-signal-clarity": {
        "title": "Dashboard Signal Clarity",
        "summary": "约束仪表盘的信息密度、交互优先级和图表可读性，避免漂亮但没信号的后台页。",
        "category": "dashboard",
        "tags": ["dashboard", "analytics", "admin"],
    },
    "source-first-research-loop": {
        "title": "Source-First Research Loop",
        "summary": "优先从 GitHub 源码、官方文档、教程和复盘中提炼方案，再用少量成品站点做风格佐证，减少 analyst 被实体网站和截图盲区带偏。",
        "category": "research",
        "tags": ["research", "github", "docs", "planning"],
        "source_name": "agency-agents + AutoRA",
        "source_url": "https://github.com/msitarzewski/agency-agents",
        "license_note": "借鉴多智能体 SOP 和研究循环方法，输出为 Evermind 自定义工作流约束，不复制上游实现。",
        "example_goal": "先研究高质量官网源码、教程和实现拆解，再输出给 builder 的执行蓝图",
    },
    "scroll-evidence-capture": {
        "title": "Scroll Evidence Capture",
        "summary": "要求 reviewer/tester/polisher 用持续滚动证据覆盖页面顶部、中段和底部，避免只看首屏就误判通过。",
        "category": "qa",
        "tags": ["qa", "scroll", "browser", "evidence"],
        "source_name": "rrweb + Lightpanda",
        "source_url": "https://github.com/rrweb-io/rrweb",
        "license_note": "借鉴滚动回放和轻量浏览自动化思路，只写审查准则与工具约束，不直接搬运上游录制代码。",
        "example_goal": "录制整页滚动并确认中间段落、底部 CTA 和跨页动效没有退化",
    },
    "visual-slot-recovery": {
        "title": "Visual Slot Recovery",
        "summary": "约束 polisher 主动清理空白图片区、占位图文案和假视觉块，把未完成的媒体模块补成真正可交付的视觉内容。",
        "category": "ui",
        "tags": ["website", "polish", "images", "visuals"],
        "source_name": "OpenClaw + commercial website QA patterns",
        "source_url": "https://github.com/",
        "license_note": "借鉴成品站点补图和视觉完工检查思路，仅写 Evermind 内部提示技能，不复制上游实现。",
        "example_goal": "把 builder 留下的空白 hero 图、集合卡片图和地图占位块补成真实视觉并统一动效节奏",
    },
    "gameplay-qa-gate": {
        "title": "Gameplay QA Gate",
        "summary": "要求 reviewer/tester 真正点击开始并试玩，而不是只看首屏截图。",
        "category": "qa",
        "tags": ["game", "qa", "browser"],
        "source_name": "Playwright + browser gameplay QA patterns",
        "source_url": "https://github.com/microsoft/playwright",
        "license_note": "参考可重复浏览器自动化与交互验证纪律，整理为 Evermind 的游戏 QA 约束，不复制上游实现。",
        "example_goal": "真正点击开始并试玩第三人称射击游戏，验证视角、开火、弹道与结算流程",
    },
    "pptx-export-bridge": {
        "title": "PPTX Export Bridge",
        "summary": "把演示内容组织成可导出 PPT/Pitch Deck 的结构，适合融资路演和产品发布。",
        "category": "presentation",
        "tags": ["ppt", "slides", "deck"],
        "source_name": "PptxGenJS + reveal.js + Marp",
        "source_url": "https://github.com/gitbrent/PptxGenJS",
        "license_note": "借鉴程序化 PPT 导出、浏览器演示与 Markdown 幻灯片工作流，只输出 Evermind 的结构化 deck 约束，不搬运上游实现。",
        "example_goal": "做一个可打印、可导出、可继续转成 PPTX 的融资路演 deck",
    },
    "comfyui-pipeline-brief": {
        "title": "ComfyUI Pipeline Brief",
        "summary": "把图像生成需求整理成可落地的节点式图片工作流简报，适合海报、封面和角色图。",
        "category": "media",
        "tags": ["image", "workflow", "comfyui"],
    },
    "review-escalation-computer-use": {
        "title": "Review Escalation Computer Use",
        "summary": "在条件允许时，把复杂 QA 提升到更强的电脑操作模式，用于补强最后一道质量门。",
        "category": "qa",
        "tags": ["qa", "computer-use", "browser"],
        "source_name": "OpenHands + Playwright escalation patterns",
        "source_url": "https://github.com/All-Hands-AI/OpenHands",
        "license_note": "参考更强桌面代理与浏览器执行协同方式，重写为 Evermind 的审查升级准则，不复制上游实现。",
        "example_goal": "当普通浏览器证据不足时，升级到更强交互模式确认复杂桌面/画布行为",
    },
    "remotion-scene-composer": {
        "title": "Remotion Scene Composer",
        "summary": "受 Remotion 工作流启发，把视频需求拆成镜头、时长、转场、字幕和渲染组合。",
        "category": "video",
        "tags": ["video", "storyboard", "timeline", "motion"],
        "source_name": "Remotion",
        "source_url": "https://github.com/remotion-dev/remotion",
        "license_note": "参考了 Remotion 的组合式视频工作流思路；不要直接复制其受限制代码或品牌资产。",
        "example_goal": "做一个 30 秒产品宣传短片 storyboard 和分镜脚本",
    },
    "ltx-cinematic-video-blueprint": {
        "title": "LTX Cinematic Video Blueprint",
        "summary": "受 LTX Studio / LTX-Video 启发，强调镜头连续性、运动描述、主体一致性和可生成的 shot prompt。",
        "category": "video",
        "tags": ["video", "cinematic", "shot", "prompt"],
        "source_name": "LTX Studio / LTX-Video",
        "source_url": "https://github.com/Lightricks/LTX-Desktop",
        "license_note": "参考其镜头规划与视频生成产品思路，采用自写提示词和结构，不搬运原项目实现。",
        "example_goal": "生成电影感产品广告镜头表、shot prompt 和 continuity notes",
    },
    "godogen-playable-loop": {
        "title": "Godogen Playable Loop",
        "summary": "受 Godogen 启发，要求游戏任务先交付可玩的核心循环，同时保留视觉目标、控制手感调优和截图/试玩修复闭环。",
        "category": "game",
        "tags": ["game", "vertical-slice", "qa", "loop"],
        "source_name": "Godogen",
        "source_url": "https://github.com/htdt/godogen",
        "license_note": "参考其 agentic game pipeline 思路，采用 Evermind 自己的任务拆解与 QA 闭环。",
        "example_goal": "做一个能立即上手玩的 2D 小游戏 vertical slice",
    },
    "godogen-tps-control-sanity-lock": {
        "title": "Godogen TPS Control Sanity Lock",
        "summary": "把第三人称/TPS 的 WASD、右向量和拖拽视角方向锁成统一契约，避免左右镜像和俯仰反向。",
        "category": "game",
        "tags": ["game", "tps", "controls", "camera", "qa"],
        "source_name": "Godogen + ecctrl + GDQuest",
        "source_url": "https://github.com/pmndrs/ecctrl",
        "license_note": "参考 Godogen 的 playable/repair loop，以及 ecctrl、GDQuest 的第三人称控制约定，重写为 Evermind 内部控制契约技能。",
        "example_goal": "让第三人称射击游戏稳定满足 A 左 D 右、右拖右转、上拖不反向的商业级操作手感",
    },
    "godogen-visual-target-lock": {
        "title": "Godogen Visual Target Lock",
        "summary": "把 3D 游戏的主角、敌人、武器、场景和 HUD 锁进同一套视觉目标，避免 builder 东拼西凑。",
        "category": "game",
        "tags": ["game", "3d", "art-direction", "style-lock"],
        "source_name": "Godogen",
        "source_url": "https://github.com/htdt/godogen",
        "license_note": "参考其 visual target / art direction 锁定思路，重写为 Evermind 内部提示技能。",
        "example_goal": "为第三人称 3D 射击游戏锁定人物、怪物、枪械、场景和 HUD 的统一视觉方向",
    },
    "godogen-3d-asset-replacement": {
        "title": "Godogen 3D Asset Replacement",
        "summary": "要求 3D 游戏把占位物、真实资产、替换清单和运行时绑定分离，后续换模不必重写玩法。",
        "category": "game",
        "tags": ["game", "3d", "assets", "replacement"],
        "source_name": "Godogen",
        "source_url": "https://github.com/htdt/godogen",
        "license_note": "参考其 asset planner / asset gen / runtime replacement 流程，采用 Evermind 自己的 manifest 与替换规则。",
        "example_goal": "让第三人称射击游戏先用可信占位物开玩，再平滑替换成正式人物、怪物和武器资产",
    },
    "premium-typography-system": {
        "title": "Premium Typography System",
        "summary": "Evermind 自研排版引擎，融合 CJK 排版最佳实践、流式字体缩放和视觉层级系统，让生成的网页排版达到专业级水准。",
        "category": "ui",
        "tags": ["website", "typography", "CJK", "layout"],
        "source_name": "Evermind Typography Engine",
        "source_url": "",
        "license_note": "Evermind 自研设计约束系统，融合 utopia.fyi 流式排版理念和 CJK 最佳实践。",
        "example_goal": "生成中文官网时自动应用排版规范、行高、字间距和容器内边距",
    },
    "cinematic-visual-narrative": {
        "title": "Cinematic Visual Narrative",
        "summary": "Evermind 自研电影级视觉叙事引擎，全屏英雄区 + 视差滚动 + 渐进式内容揭示 + 电影色彩系统。",
        "category": "ui",
        "tags": ["website", "cinematic", "visual", "hero"],
        "source_name": "Evermind Visual Engine",
        "source_url": "",
        "license_note": "Evermind 自研，融合 Awwwards 顶级网站设计模式和 glassmorphism 效果体系。",
        "example_goal": "创建全屏沉浸式着陆页，带视差、渐入动效和暗色电影调色",
    },
    "responsive-grid-mastery": {
        "title": "Responsive Grid Mastery",
        "summary": "Evermind 自研自适应布局系统，基于 CSS Grid + Container Queries 的高级响应式方案。",
        "category": "ui",
        "tags": ["website", "responsive", "grid", "layout"],
        "source_name": "Evermind Layout Engine",
        "source_url": "",
        "license_note": "Evermind 自研，融合 open-props/utopia.fyi 设计 token 系统理念。",
        "example_goal": "创建完美适配手机到桌面的多列自适应网格布局",
    },
    "immersive-scroll-interactions": {
        "title": "Immersive Scroll Interactions",
        "summary": "Evermind 自研沉浸式滚动交互引擎，IntersectionObserver + CSS scroll-driven 动画 + 交错卡片入场。",
        "category": "ui",
        "tags": ["website", "scroll", "animation", "interaction"],
        "source_name": "Evermind Scroll Engine",
        "source_url": "",
        "license_note": "Evermind 自研，融合 GSAP ScrollTrigger 交互模式，纯 CSS/JS 实现无外部依赖。",
        "example_goal": "添加滚动进度条、交错卡片入场动画和数字计数器效果",
    },
    "evermind-atlas-surface-system": {
        "title": "Atlas Surface System",
        "summary": "把页面从单调黑白底升级为层次化表面系统：多层背景、雾化渐变、纹理噪点与更稳定的前景/卡片对比。",
        "category": "ui",
        "tags": ["website", "palette", "background", "surfaces"],
        "source_name": "Evermind Atlas System · adapted from Open Props + Pattern Craft",
        "source_url": "https://github.com/argyleink/open-props",
        "license_note": "吸收 Open Props 的 token/surface 组织方式与 Pattern Craft 的纹理背景思路，重写为 Evermind 内部设计约束，不复制上游实现。",
        "example_goal": "让电影感官网摆脱纯黑纯白底，形成前景/中景/背景三层表面系统",
    },
    "evermind-editorial-layout-composer": {
        "title": "Editorial Layout Composer",
        "summary": "约束导航、Hero、媒体卡片和正文区块采用更像编辑设计的版式，而不是大图乱铺或单一网格平铺。",
        "category": "ui",
        "tags": ["website", "layout", "editorial", "navigation"],
        "source_name": "Evermind Editorial Layouts · adapted from phuocng/csslayout",
        "source_url": "https://github.com/phuocng/csslayout",
        "license_note": "借鉴 csslayout 的媒体栅格、吸附式导航和布局模式，整理成 Evermind 的多页网站布局规则。",
        "example_goal": "让旅游站的导航、媒体模块和内容节奏更像高端杂志而不是模板站",
    },
    "evermind-resilient-media-delivery": {
        "title": "Resilient Media Delivery",
        "summary": "强化图片加载、懒加载、错误回退和媒体容器约束，避免坏图、错图、超大图和背景图空白导致 Reviewer 卡死。",
        "category": "media",
        "tags": ["website", "images", "lazyload", "fallback"],
        "source_name": "Evermind Media Delivery · adapted from vanilla-lazyload",
        "source_url": "https://github.com/verlok/vanilla-lazyload",
        "license_note": "吸收 vanilla-lazyload 的延迟加载与失败恢复思路，改写为静态 HTML/CSS/JS 可直接落地的 Evermind 媒体规范。",
        "example_goal": "让多页官网里的 Hero 图、卡片图和头像在弱网或外链失败时也保持可交付",
    },
    "evermind-review-remediation-gate": {
        "title": "Review Remediation Gate",
        "summary": "要求 reviewer/tester 不只指出问题，还要输出可执行的回退/整改 brief，并且在网站质量不达标时阻止劣质产物继续流出。",
        "category": "qa",
        "tags": ["website", "review", "remediation", "rollback"],
        "source_name": "Evermind Review Gate",
        "source_url": "",
        "license_note": "Evermind 自研质量门与整改协议，针对多页网站的导航、版式、背景和媒体一致性进行强制打回。",
        "example_goal": "让 reviewer 在发现错图、巨图、导航断裂或背景平面化时给出精确返工说明并阻断交付",
    },
}


def _clear_skill_caches() -> None:
    _load_skill.cache_clear()
    list_skill_catalog.cache_clear()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or f"skill-{int(time.time())}"


def _humanize_skill_name(name: str) -> str:
    return " ".join(part.capitalize() for part in str(name or "").replace("_", "-").split("-") if part)


def _ensure_user_skills_dir() -> None:
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _skill_locations() -> List[Tuple[Path, str]]:
    return [(SKILLS_DIR, "builtin"), (USER_SKILLS_DIR, "community")]


def _resolve_skill_dir(name: str) -> Tuple[Path | None, str]:
    skill_name = str(name or "").strip()
    if not skill_name:
        return None, ""
    for base_dir, origin in _skill_locations():
        candidate = base_dir / skill_name
        if (candidate / "SKILL.md").exists():
            return candidate, origin
    return None, ""


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    raw = str(text or "")
    # Opus R4 fix: accept CRLF-authored skills (Windows contributors).
    if not (raw.startswith("---\n") or raw.startswith("---\r\n")):
        return {}, raw
    # Try both separator variants
    parts = raw.split("\n---\n", 1)
    if len(parts) != 2:
        parts = raw.split("\r\n---\r\n", 1)
    if len(parts) != 2:
        return {}, raw
    meta_block, body = parts
    meta: Dict[str, str] = {}
    for line in meta_block.splitlines()[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("'").strip('"')
    return meta, body.strip()


def _extract_skill_preview(body: str) -> str:
    _meta, clean_body = _parse_frontmatter(body)
    lines = [line.strip("-• ").strip() for line in str(clean_body or "").splitlines()]
    for line in lines:
        if not line:
            continue
        if line.isupper() and len(line) < 80:
            continue
        return line[:220]
    return ""


def _load_sidecar_metadata(skill_dir: Path) -> Dict[str, Any]:
    raw = _read_text_file(skill_dir / COMMUNITY_META_FILE)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _infer_skill_node_types(name: str) -> List[str]:
    selected: List[str] = []
    for node_type, mapping in SKILL_MAP.items():
        for skill_names in mapping.values():
            if name in skill_names and node_type not in selected:
                selected.append(node_type)
    for rule in GOAL_SKILL_RULES:
        for node_type, skill_names in (rule.get("skills") or {}).items():
            if name in skill_names and node_type not in selected:
                selected.append(node_type)
    return selected


def _infer_skill_category(name: str, tags: List[str]) -> str:
    hint = SKILL_LIBRARY_HINTS.get(name, {})
    if hint.get("category"):
        return str(hint["category"])
    joined = " ".join([name] + list(tags)).lower()
    if any(token in joined for token in ("video", "motion", "storyboard", "shot")):
        return "video"
    if any(token in joined for token in ("game", "sprite", "pixel")):
        return "game"
    if any(token in joined for token in ("dashboard", "analytics", "admin")):
        return "dashboard"
    if any(token in joined for token in ("ppt", "slides", "deck")):
        return "presentation"
    if any(token in joined for token in ("image", "illustration", "asset", "media")):
        return "media"
    if any(token in joined for token in ("qa", "review", "test")):
        return "qa"
    return "general"


def _build_skill_record(name: str, skill_dir: Path, origin: str) -> Dict[str, Any]:
    body = _read_text_file(skill_dir / "SKILL.md")
    frontmatter, clean_body = _parse_frontmatter(body)
    sidecar = _load_sidecar_metadata(skill_dir)
    hints = SKILL_LIBRARY_HINTS.get(name, {})
    title = str(sidecar.get("title") or hints.get("title") or frontmatter.get("name") or _humanize_skill_name(name))
    tags_raw = sidecar.get("tags") if isinstance(sidecar.get("tags"), list) else hints.get("tags") or []
    tags = [str(item).strip() for item in tags_raw if str(item).strip()]
    summary = str(sidecar.get("summary") or hints.get("summary") or frontmatter.get("description") or _extract_skill_preview(clean_body) or f"{title} skill").strip()
    keywords_raw = sidecar.get("keywords") if isinstance(sidecar.get("keywords"), list) else []
    keywords = [str(item).strip().lower() for item in keywords_raw if str(item).strip()]
    node_types_raw = sidecar.get("node_types") if isinstance(sidecar.get("node_types"), list) else hints.get("node_types") or []
    node_types = [str(item).strip().lower() for item in node_types_raw if str(item).strip()]
    if not node_types:
        node_types = _infer_skill_node_types(name)
    category = str(sidecar.get("category") or _infer_skill_category(name, tags))
    installed_at = int(sidecar.get("installed_at") or skill_dir.stat().st_mtime)
    record: Dict[str, Any] = {
        "name": name,
        "title": title[:120],
        "summary": summary[:240],
        "category": category[:40],
        "tags": tags[:12],
        "keywords": keywords[:24],
        "node_types": node_types[:12],
        "origin": origin,
        "source_name": str(sidecar.get("source_name") or hints.get("source_name") or ("Evermind Built-in" if origin == "builtin" else "Community Skill")),
        "source_url": str(sidecar.get("source_url") or hints.get("source_url") or ""),
        "license_note": str(sidecar.get("license_note") or hints.get("license_note") or ""),
        "example_goal": str(sidecar.get("example_goal") or hints.get("example_goal") or ""),
        "installed_at": installed_at,
    }
    return record


@lru_cache(maxsize=1)
def list_skill_catalog() -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    for base_dir, origin in _skill_locations():
        if not base_dir.exists():
            continue
        for skill_file in sorted(base_dir.glob("*/SKILL.md")):
            catalog.append(_build_skill_record(skill_file.parent.name, skill_file.parent, origin))
    catalog.sort(key=lambda item: (item.get("origin") != "builtin", str(item.get("category") or ""), str(item.get("name") or "")))
    return catalog


@lru_cache(maxsize=128)
def _load_skill(name: str) -> str:
    # v4.0: Load from local skills/ directory first (transplanted skill files)
    _local_skill_dir = Path(__file__).parent / "skills"
    _local_path = _local_skill_dir / f"{name}.md"
    if _local_path.exists():
        try:
            return _local_path.read_text(encoding="utf-8")
        except Exception:
            pass
    skill_dir, _origin = _resolve_skill_dir(name)
    if not skill_dir:
        return ""
    return _read_text_file(skill_dir / "SKILL.md")


def _resolve_skill_names(node_type: str, task_type: str) -> List[str]:
    selected: List[str] = []
    mapping = SKILL_MAP.get(str(node_type or "").strip().lower(), {})
    for bucket in ("*", str(task_type or "").strip().lower()):
        for name in mapping.get(bucket, []):
            if name and name not in selected:
                selected.append(name)
    return selected


def _resolve_goal_keyword_skills(node_type: str, goal: str) -> List[str]:
    node_key = str(node_type or "").strip().lower()
    goal_text = str(goal or "").strip()
    selected: List[str] = []
    if not node_key or not goal_text:
        return selected
    for rule in GOAL_SKILL_RULES:
        pattern = rule.get("pattern")
        if not pattern or not pattern.search(goal_text):
            continue
        skills_by_node = rule.get("skills", {})
        for name in skills_by_node.get(node_key, []):
            if name and name not in selected:
                selected.append(name)
    return selected


def _resolve_frontmatter_skills(node_type: str, goal: str) -> List[str]:
    """v6.1.6 (Antigravity-style): scan SKILL.md frontmatter for
    ``activation_triggers`` / ``activation_keywords`` / ``activation_node_types``.

    Builtin and community skills both eligible. Frontmatter example:

        ---
        name: my-skill
        activation_node_types: builder, debugger
        activation_triggers: fps, pointer lock, three.js
        ---

    Returns skills whose triggers substring-match the goal text for the given
    node type. Lets skill authors declare "when to load me" without touching
    the hardcoded GOAL_SKILL_RULES list.
    """
    node_key = str(node_type or "").strip().lower()
    goal_text = str(goal or "").strip().lower()
    selected: List[str] = []
    if not node_key or not goal_text:
        return selected

    def _visit(directory: Path) -> None:
        if not directory.exists():
            return
        for skill_dir in directory.iterdir():
            if not skill_dir.is_dir():
                continue
            body = _read_text_file(skill_dir / "SKILL.md")
            if not body:
                continue
            meta, _ = _parse_frontmatter(body)
            if not meta:
                continue
            # Check node_types allowlist (comma-separated)
            raw_nodes = str(meta.get("activation_node_types") or "").strip()
            if raw_nodes and raw_nodes != "*":
                allowed = {t.strip().lower() for t in raw_nodes.split(",") if t.strip()}
                if node_key not in allowed:
                    continue
            # Check trigger keywords — any match fires
            raw_triggers = " ".join(
                str(meta.get(k) or "")
                for k in ("activation_triggers", "activation_keywords", "triggers", "keywords")
            )
            if not raw_triggers.strip():
                continue
            tokens = [t.strip().lower() for t in re.split(r"[,;]", raw_triggers) if t.strip()]
            if any(tok and tok in goal_text for tok in tokens):
                name = skill_dir.name
                if name and name not in selected:
                    selected.append(name)

    _visit(SKILLS_DIR)
    _visit(USER_SKILLS_DIR)
    return selected


def _resolve_community_skills(node_type: str, goal: str) -> List[str]:
    node_key = str(node_type or "").strip().lower()
    goal_text = str(goal or "").strip().lower()
    selected: List[str] = []
    if not node_key or not goal_text:
        return selected
    for record in list_skill_catalog():
        if record.get("origin") != "community":
            mentioned_tokens = [
                str(record.get("name") or "").strip().lower(),
                str(record.get("title") or "").strip().lower(),
            ]
            if any(token and token in goal_text for token in mentioned_tokens):
                if str(record.get("name") or "") not in selected:
                    selected.append(str(record.get("name") or ""))
            continue

        allowed_nodes = [str(item).strip().lower() for item in (record.get("node_types") or []) if str(item).strip()]
        if allowed_nodes and "*" not in allowed_nodes and node_key not in allowed_nodes:
            continue
        terms = [str(record.get("name") or "").lower(), str(record.get("title") or "").lower()]
        terms.extend([str(item).lower() for item in (record.get("keywords") or []) if str(item).strip()])
        terms.extend([str(item).lower() for item in (record.get("tags") or []) if str(item).strip()])
        if any(term and term in goal_text for term in terms):
            skill_name = str(record.get("name") or "")
            if skill_name and skill_name not in selected:
                selected.append(skill_name)
    return selected


def resolve_skill_names_for_goal(node_type: str, goal: str) -> List[str]:
    node_key = normalize_node_role(node_type)
    goal_text = str(goal or "").strip()
    if not node_key or not goal_text:
        return []
    try:
        task_type = task_classifier.classify(goal_text).task_type
    except Exception:
        task_type = "website"

    # P1 FIX: Apply task-type exclusion to prevent cross-domain skill contamination.
    # e.g. website tasks should never load game-specific skills like gameplay-foundation.
    excluded = set(SKILL_EXCLUSION_MAP.get(task_type, set()))
    if task_type == "game":
        runtime_mode = task_classifier.game_runtime_mode(goal_text)
        asset_mode = task_classifier.game_asset_pipeline_mode(goal_text)
        if runtime_mode == "3d_engine" or asset_mode == "3d":
            # Premium 3D game briefs should still keep the playable-loop and
            # QA discipline from Godogen, but must not inherit the pixel /
            # low-fidelity asset defaults.
            excluded.update({"pixel-asset-pipeline"})
        if asset_mode == "3d":
            # 3D asset-design packs should stay in modeling-brief mode unless a
            # real image backend is explicitly being used elsewhere.
            excluded.add("comfyui-pipeline-brief")

    selected: List[str] = []
    for name in (
        _resolve_skill_names(node_key, task_type)
        + _resolve_goal_keyword_skills(node_key, goal_text)
        + _resolve_frontmatter_skills(node_key, goal_text)  # v6.1.6
        + _resolve_community_skills(node_key, goal_text)
    ):
        if name and name not in selected and name not in excluded:
            selected.append(name)
    if node_key == "uidesign" and task_type == "website":
        core_uidesign_skills = {"commercial-ui-polish", "design-system-consistency"}
        selected = [name for name in selected if name in core_uidesign_skills]
        if not selected:
            selected = ["commercial-ui-polish", "design-system-consistency"]
    return selected


def resolve_skill_records_for_goal(node_type: str, goal: str) -> List[Dict[str, Any]]:
    records_by_name = {
        str(record.get("name") or "").strip(): record
        for record in list_skill_catalog()
        if str(record.get("name") or "").strip()
    }
    resolved: List[Dict[str, Any]] = []
    for name in resolve_skill_names_for_goal(node_type, goal):
        record = records_by_name.get(name)
        if record:
            resolved.append(record)
            continue
        resolved.append({
            "name": name,
            "title": _humanize_skill_name(name),
            "summary": "",
            "category": "general",
            "tags": [],
            "keywords": [],
            "node_types": [],
            "origin": "builtin",
            "source_name": "Evermind Built-in",
            "source_url": "",
            "license_note": "",
            "example_goal": "",
            "installed_at": 0,
        })
    return resolved


def list_available_skill_names() -> List[str]:
    return [str(record.get("name") or "") for record in list_skill_catalog() if str(record.get("name") or "")]


def build_skill_context(node_type: str, goal: str, *, budget_chars: int = 9000) -> str:
    """Build skill context for a node, respecting a character budget.

    V4.3: Skills were adding 8,000-13,500 chars to system prompts with no cap,
    inflating TTFT by 2-5x.  Now loads skills in priority order (first resolved
    = highest priority) and stops when the budget is exhausted.  Default 9000
    chars (~2250 tokens) keeps core + domain-specific skills while trimming
    the long tail (e.g. godogen-visual-target-lock, godogen-3d-asset-replacement).

    v5.8.6 per-node budget override: planner/analyst are text-production
    orchestration nodes and don't need multiple behavioural skills — they
    get a tight budget so Kimi prefill shrinks ~40-60s per call. Builder /
    reviewer keep the full 9000 because they actually execute tools and
    need the skill directives.
    """
    # Per-node budget override (v5.8.6).
    normalized = str(node_type or "").strip().lower()
    _NODE_BUDGET = {
        "planner":   2500,   # ~625 tokens — keep 1 core skill
        "analyst":   4500,   # ~1125 tokens — analyst needs research skills
        "router":    1500,   # router is trivial routing, no skills needed
        "scribe":    3000,
        "uidesign":  3000,
        "deployer":  2500,
    }
    effective_budget = _NODE_BUDGET.get(normalized, budget_chars)

    parts: List[str] = []
    total = 0
    for name in resolve_skill_names_for_goal(node_type, goal):
        body = _load_skill(name)
        if not body:
            continue
        entry = f"[Skill: {name}]\n{body}"
        if total + len(entry) > effective_budget and parts:
            # Already have at least one skill; stop to stay within budget.
            break
        parts.append(entry)
        total += len(entry)
    return "\n\n".join(parts)


def _http_get_bytes(url: str) -> bytes:
    req = Request(url, headers=HTTP_HEADERS)
    with urlopen(req, timeout=20) as resp:
        return resp.read()


def _http_get_json(url: str) -> Any:
    data = _http_get_bytes(url)
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON from {url}: {exc}") from exc


def _github_tree_parts(source_url: str) -> Tuple[str, str, str, str]:
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) >= 5 and parts[2] in {"tree", "blob"}:
        owner, repo, mode, ref = parts[:4]
        repo_path = "/".join(parts[4:])
        return owner, repo, ref, repo_path if mode == "tree" else repo_path
    raise ValueError("Unsupported GitHub URL. Use a folder URL ending with /tree/<ref>/<path> or a file URL to SKILL.md.")


def _download_github_directory(owner: str, repo: str, ref: str, repo_path: str, dest_dir: Path, total_size: List[int], depth: int = 0) -> None:
    if depth > 4:
        raise ValueError("Skill folder is nested too deeply to install safely.")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(repo_path, safe='/')}?ref={quote(ref, safe='')}"
    payload = _http_get_json(api_url)
    if not isinstance(payload, list):
        raise ValueError("GitHub folder listing did not return a directory.")
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        item_type = str(item.get("type") or "").strip()
        if not name or name.startswith("."):
            continue
        # v7.3.9 audit-fix CRITICAL — never trust the GitHub API's `name`
        # field verbatim. A malicious/compromised mirror could return
        # `name = "../../../tmp/evil.py"` (path traversal) or contain a
        # forward slash that escapes `dest_dir`. Reject anything that
        # contains slashes, parent-dir tokens, or NUL bytes.
        if "/" in name or "\\" in name or ".." in name or "\x00" in name or name.startswith("/"):
            continue
        target = dest_dir / name
        # Defence in depth: resolved path must stay within dest_dir.
        try:
            if not str(target.resolve()).startswith(str(dest_dir.resolve()) + os.sep):
                continue
        except Exception:
            continue
        if item_type == "dir":
            target.mkdir(parents=True, exist_ok=True)
            child_path = str(item.get("path") or "").strip()
            if child_path:
                _download_github_directory(owner, repo, ref, child_path, target, total_size, depth + 1)
            continue
        if item_type != "file":
            continue
        ext = target.suffix.lower()
        if ext not in ALLOWED_INSTALL_EXTENSIONS and name != "SKILL.md":
            continue
        size = int(item.get("size") or 0)
        if size > GITHUB_FILE_SIZE_LIMIT:
            raise ValueError(f"File too large for skill install: {name}")
        total_size[0] += max(size, 0)
        if total_size[0] > GITHUB_TOTAL_SIZE_LIMIT:
            raise ValueError("Skill folder exceeds the safe community install size limit.")
        download_url = str(item.get("download_url") or "").strip()
        if not download_url:
            continue
        # v7.3.9 audit-fix CRITICAL — pin download host to GitHub's content
        # CDN. The Contents API normally returns
        # https://raw.githubusercontent.com/<owner>/<repo>/... but a
        # malicious mirror could serve a redirect to evil.com. Verify the
        # URL stays inside the GitHub host whitelist.
        try:
            _du = urlparse(download_url)
            _allowed_dl_hosts = {
                "raw.githubusercontent.com",
                "github.com",
                "objects.githubusercontent.com",
                "codeload.github.com",
            }
            if _du.scheme not in {"http", "https"} or _du.netloc not in _allowed_dl_hosts:
                continue
        except Exception:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_http_get_bytes(download_url))


def install_skill_from_github(
    source_url: str,
    requested_name: str = "",
    title: str = "",
    summary: str = "",
    category: str = "",
    node_types: List[str] | None = None,
    keywords: List[str] | None = None,
    tags: List[str] | None = None,
) -> Dict[str, Any]:
    url = str(source_url or "").strip()
    if not url:
        raise ValueError("source_url is required")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https GitHub URLs are supported")
    if parsed.netloc not in {"github.com", "raw.githubusercontent.com"}:
        raise ValueError("Only public GitHub URLs are supported for community skill install")

    _ensure_user_skills_dir()

    if parsed.netloc == "raw.githubusercontent.com":
        raw_parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(raw_parts) < 5 or raw_parts[-1] != "SKILL.md":
            raise ValueError("Raw GitHub URL must point directly to SKILL.md")
        derived_name = raw_parts[-2]
    else:
        owner, repo, _ref, repo_path = _github_tree_parts(url)
        derived_name = Path(repo_path).parent.name if repo_path.endswith("/SKILL.md") else (Path(repo_path).name or repo)

    skill_name = _slugify(requested_name or derived_name)
    if _resolve_skill_dir(skill_name)[0]:
        raise ValueError(f"Skill '{skill_name}' already exists")

    temp_dir = USER_SKILLS_DIR / f".tmp_{skill_name}_{int(time.time())}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        if parsed.netloc == "raw.githubusercontent.com":
            (temp_dir / "SKILL.md").write_bytes(_http_get_bytes(url))
        else:
            owner, repo, ref, repo_path = _github_tree_parts(url)
            if repo_path.endswith("/SKILL.md"):
                skill_raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{repo_path}"
                (temp_dir / "SKILL.md").write_bytes(_http_get_bytes(skill_raw_url))
            else:
                _download_github_directory(owner, repo, ref, repo_path, temp_dir, [0])

        skill_body = _read_text_file(temp_dir / "SKILL.md")
        if not skill_body:
            raise ValueError("Installed folder does not contain a readable SKILL.md")
        frontmatter, clean_body = _parse_frontmatter(skill_body)

        meta = {
            "title": str(title or frontmatter.get("name") or _humanize_skill_name(skill_name))[:120],
            "summary": str(summary or frontmatter.get("description") or _extract_skill_preview(clean_body) or "Community skill")[:240],
            "category": str(category or "community")[:40],
            "node_types": [str(item).strip().lower() for item in (node_types or []) if str(item).strip()][:12],
            "keywords": [str(item).strip().lower() for item in (keywords or []) if str(item).strip()][:24],
            "tags": [str(item).strip() for item in (tags or []) if str(item).strip()][:16],
            "source_url": url,
            "source_name": "GitHub Community Skill",
            "license_note": "Imported from a public GitHub skill folder. Review the upstream license before redistribution.",
            "installed_at": int(time.time()),
        }
        (temp_dir / COMMUNITY_META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        final_dir = USER_SKILLS_DIR / skill_name
        temp_dir.rename(final_dir)
    except (HTTPError, URLError) as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ValueError(f"Failed to download GitHub skill: {exc}") from exc
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    _clear_skill_caches()
    record = next((item for item in list_skill_catalog() if item.get("name") == skill_name), None)
    if not record:
        raise ValueError("Skill installed but could not be indexed")
    return record


def remove_installed_skill(name: str) -> bool:
    skill_name = str(name or "").strip()
    if not skill_name:
        return False
    skill_dir = USER_SKILLS_DIR / skill_name
    if not (skill_dir / "SKILL.md").exists():
        return False
    shutil.rmtree(skill_dir, ignore_errors=True)
    _clear_skill_caches()
    return True
