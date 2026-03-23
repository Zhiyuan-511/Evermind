"""
Evermind Agent Skills

Built-in prompt skills live under backend/agent_skills/<skill-name>/SKILL.md.
User-installed community skills live under ~/.evermind/skills/<skill-name>/SKILL.md.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
import shutil
import time
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

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
        "*": ["commercial-ui-polish", "ui-polish-microstates"],
        "website": ["commercial-ui-polish", "conversion-surface-architecture", "motion-choreography-system"],
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
        "website": ["commercial-ui-polish", "motion-choreography-system", "svg-illustration-system"],
        "dashboard": ["dashboard-signal-clarity", "data-storytelling-panels", "design-system-consistency"],
        "presentation": ["slides-story-arc", "motion-choreography-system"],
        "creative": ["motion-choreography-system", "svg-illustration-system", "remotion-scene-composer"],
    },
    "imagegen": {
        "*": ["image-prompt-director", "visual-storyboard-shotlist", "comfyui-pipeline-brief"],
        "website": ["image-prompt-director", "svg-illustration-system"],
        "game": ["image-prompt-director", "pixel-asset-pipeline", "visual-storyboard-shotlist"],
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
        "*": ["browser-observe-act-verify", "evidence-driven-qa", "design-system-consistency"],
        "website": ["browser-observe-act-verify", "evidence-driven-qa", "design-system-consistency"],
        "dashboard": ["dashboard-signal-clarity", "evidence-driven-qa", "design-system-consistency"],
        "game": ["gameplay-qa-gate", "evidence-driven-qa", "review-escalation-computer-use", "godogen-playable-loop"],
        "tool": ["browser-observe-act-verify", "evidence-driven-qa", "docs-clarity-architecture"],
        "presentation": ["browser-observe-act-verify", "evidence-driven-qa", "slides-story-arc"],
        "creative": ["browser-observe-act-verify", "evidence-driven-qa", "motion-choreography-system", "remotion-scene-composer"],
    },
    "tester": {
        "*": ["browser-observe-act-verify", "evidence-driven-qa", "design-system-consistency"],
        "website": ["browser-observe-act-verify", "evidence-driven-qa", "design-system-consistency"],
        "dashboard": ["dashboard-signal-clarity", "evidence-driven-qa", "design-system-consistency"],
        "game": ["gameplay-qa-gate", "evidence-driven-qa", "review-escalation-computer-use", "godogen-playable-loop"],
        "tool": ["browser-observe-act-verify", "evidence-driven-qa", "docs-clarity-architecture"],
        "presentation": ["browser-observe-act-verify", "evidence-driven-qa", "slides-story-arc"],
        "creative": ["browser-observe-act-verify", "evidence-driven-qa", "motion-choreography-system", "remotion-scene-composer"],
    },
    "analyst": {
        "*": ["research-pattern-extraction"],
        "website": ["research-pattern-extraction", "conversion-surface-architecture"],
        "dashboard": ["research-pattern-extraction", "data-storytelling-panels"],
        "presentation": ["research-pattern-extraction", "slides-story-arc"],
        "game": ["research-pattern-extraction", "pixel-asset-pipeline", "godogen-playable-loop"],
        "creative": ["research-pattern-extraction", "remotion-scene-composer", "ltx-cinematic-video-blueprint"],
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
    "gameplay-qa-gate": {
        "title": "Gameplay QA Gate",
        "summary": "要求 reviewer/tester 真正点击开始并试玩，而不是只看首屏截图。",
        "category": "qa",
        "tags": ["game", "qa", "browser"],
    },
    "pptx-export-bridge": {
        "title": "PPTX Export Bridge",
        "summary": "把演示内容组织成可导出 PPT/Pitch Deck 的结构，适合融资路演和产品发布。",
        "category": "presentation",
        "tags": ["ppt", "slides", "deck"],
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
        "summary": "受 Godogen 启发，强制游戏任务先建立可玩的核心循环，再用截图/试玩反馈驱动修复。",
        "category": "game",
        "tags": ["game", "vertical-slice", "qa", "loop"],
        "source_name": "Godogen",
        "source_url": "https://github.com/htdt/godogen",
        "license_note": "参考其 agentic game pipeline 思路，采用 Evermind 自己的任务拆解与 QA 闭环。",
        "example_goal": "做一个能立即上手玩的 2D 小游戏 vertical slice",
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
    if not raw.startswith("---\n"):
        return {}, raw
    parts = raw.split("\n---\n", 1)
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
    node_key = str(node_type or "").strip().lower()
    goal_text = str(goal or "").strip()
    if not node_key or not goal_text:
        return []
    try:
        task_type = task_classifier.classify(goal_text).task_type
    except Exception:
        task_type = "website"
    selected: List[str] = []
    for name in (
        _resolve_skill_names(node_key, task_type)
        + _resolve_goal_keyword_skills(node_key, goal_text)
        + _resolve_community_skills(node_key, goal_text)
    ):
        if name and name not in selected:
            selected.append(name)
    return selected


def list_available_skill_names() -> List[str]:
    return [str(record.get("name") or "") for record in list_skill_catalog() if str(record.get("name") or "")]


def build_skill_context(node_type: str, goal: str) -> str:
    parts: List[str] = []
    for name in resolve_skill_names_for_goal(node_type, goal):
        body = _load_skill(name)
        if body:
            parts.append(f"[Skill: {name}]\n{body}")
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
        target = dest_dir / name
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
