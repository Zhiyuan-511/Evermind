"""
Task Type Classifier + Specialized Prompt Templates

Detects the user's intent from goal text and provides task-specific
design guidance, structure blueprints, and quality criteria.
"""

import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_template(name: str) -> str:
    """Load a CSS template file. Returns empty string if not found."""
    path = _TEMPLATE_DIR / name
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def load_reference_template(name: str) -> str:
    """Load an HTML reference template for analyst/builder injection."""
    path = _TEMPLATE_DIR / name
    try:
        if path.exists() and path.suffix in (".html", ".htm"):
            content = path.read_text(encoding="utf-8").strip()
            if len(content) > 12000:
                return content[:12000] + "\n<!-- truncated -->"
            return content
    except Exception:
        pass
    return ""


# Reference template registry: task_type → template filename
TEMPLATE_REGISTRY: Dict[str, str] = {
    "game_3d": "game_3d_shooter.html",
    "game_2d": "game_2d_platformer.html",
    "presentation": "presentation_reveal.html",
    "website": "website_landing.html",
    "dashboard": "dashboard_analytics.html",
}

# ─────────────────────────────────────────────────────────────────
# Task Types
# ─────────────────────────────────────────────────────────────────

class TaskProfile(NamedTuple):
    task_type: str
    role: str           # Who the builder acts as
    design_system: str  # Visual/technical design guidance
    blueprint: str      # Structure / architecture blueprint
    quality: str        # What "good" looks like
    analyst_hint: str   # Research guidance for pro mode analyst
    tester_hint: str    # What tester should check


_CHINESE_DIGIT_MAP = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_MULTI_PAGE_HINT_RE = re.compile(
    r"(multi[- ]?page|multiple pages?|多页|多页面|多个页面|多个网页|多路由|多个路由|"
    r"multi[- ]?route|multiple routes?|独立页面|独立页|独立路由|站点地图|site map)",
    re.IGNORECASE,
)

_PAGE_COUNT_RE = re.compile(
    r"(?P<count>\d+|[零一二两三四五六七八九十]{1,3})\s*(?:个)?\s*(?:页面|页|page|pages|screen|screens|route|routes)",
    re.IGNORECASE,
)

_MOTION_RICH_HINT_RE = re.compile(
    r"(高级动画|动效|动画|animation|motion|transition|过渡|scroll reveal|parallax|"
    r"cinematic|苹果|apple|奢侈|luxury|premium|editorial|hero.*滚动|hero.*motion|view transition)",
    re.IGNORECASE,
)

_TRAVEL_SITE_HINT_RE = re.compile(
    r"(travel|tour|tourism|trip|vacation|destination|destinations|itinerary|road ?trip|"
    r"旅游|旅行|景点|目的地|观光|度假|行程|攻略|游玩|自驾)",
    re.IGNORECASE,
)

_ENGLISH_OUTPUT_RE = re.compile(
    r"(英文|英语|english\b|in english|use english|all in english)",
    re.IGNORECASE,
)

_CHINESE_OUTPUT_RE = re.compile(
    r"(中文|汉语|chinese\b|in chinese|use chinese|all in chinese)",
    re.IGNORECASE,
)

_NON_GAME_GENERATED_ASSET_RE = re.compile(
    r"(海报|封面|插画|concept art|illustration|poster|角色设定|image asset|image pack|素材包|asset pack)",
    re.IGNORECASE,
)

_GAME_EXPLICIT_ASSET_PIPELINE_RE = re.compile(
    r"(spritesheet|sprite sheet|sprite atlas|tileset|tile set|tilemap|tile map|frame animation|animation frames|"
    r"pixel art assets?|pixel asset pack|game asset pack|精灵图|雪碧图|瓦片集|图块集|帧动画|动画帧|"
    r"角色素材|敌人素材|boss素材|特效素材|素材包|asset pack|"
    r"建模精细|精细建模|建模精美|精美建模|建模精致|精致建模|精致美术|精美美术|精细美术|"
    # v7.7 audit: English equivalents the audit found missing — these phrasings
    # are common in English briefs but previously routed to the bare 12-node
    # path because the regex was CN-heavy.
    r"detailed\s*modeling|fine\s*modeling|polished\s*modeling|premium\s*modeling|exquisite\s*modeling|"
    r"high[- ]?fidelity\s*(?:art|modeling|game|2d|3d)|professional\s*(?:art|sprites|2d)|polished\s*(?:art|sprites)|"
    r"pixel\s*art|polished\s*(?:retro|pixel|2d|game|sprite)|"
    r"commercial-?grade\s*(?:art|game|2d|3d|shooter|rpg|platformer|sprites)|"
    r"hand[- ]?drawn|hand[- ]?painted\s*(?:assets|art|sprites)|"
    r"high[- ]?quality\s*(?:character\s*art|sprites|2d|3d|game)|"
    r"detailed\s*(?:character\s*art|enemy\s*art|monster\s*art)|"
    r"isometric\s*tileset|rpg\s*sprites|2d\s*sprites?|2d\s*character\s*pack|"
    r"crafted\s*(?:art|sprites|enemies)|"
    # PvZ-class signals: tower defense / multiple distinct character types
    r"植物大战僵尸|pvz|plants?\s*vs\.?\s*zombies?|plants?\s*and\s*zombies?|塔防游戏|塔防|"
    r"tower\s*defense|td\s*game|wave\s*defense|horde\s*defense|"
    r"不同的怪物.*不同的植物|不同的植物.*不同的怪物|多种怪物.*多种植物|多种植物.*多种怪物|"
    r"multiple\s*(?:distinct\s*)?(?:enemies|monsters|plants|towers|characters)\s*and\s*(?:enemies|monsters|plants|towers|characters)|"
    r"varied\s*enemy\s*roster|diverse\s*(?:defenders|attackers|enemies|characters)|"
    r"角色和敌人|敌人和角色|植物和僵尸|僵尸和植物|characters?\s*and\s*enemies|heroes?\s*and\s*villains?)",
    re.IGNORECASE,
)

_GAME_PROCEDURAL_OR_3D_RE = re.compile(
    r"(minecraft|我的世界|voxel|体素|3d|three\.?d|webgl|three\.?js|建模|模型|材质球|shader|"
    r"procedural|程序生成|sandbox|沙盒|open world|开放世界|crafting|生存建造|block world|方块世界)",
    re.IGNORECASE,
)

_GAME_2D_SPRITE_OVERRIDE_RE = re.compile(
    r"(2d|二维|side[- ]?scroll|side scroller|top[- ]?down|俯视|横版|platformer|平台跳跃|metroidvania|"
    r"mario|马里奥|像素平台)",
    re.IGNORECASE,
)

_GAME_3D_ASSET_PIPELINE_RE = re.compile(
    r"(3d asset(?: pack)?|3d model(?: pack)?|3d character model|3d enemy model|3d weapon model|"
    r"3d concept(?: art)?(?: pack| sheet| turnaround)?|concept(?:ual)? asset(?: pack)?|concept sheet|turnaround sheet|"
    r"model pack|weapon model|character model|enemy model|boss model|texture pack|material pack|"
    r"environment pack|prop pack|gltf\b|glb\b|fbx\b|obj\b|rigging|animation clip|voxel asset|"
    r"角色模型|怪物模型|武器模型|boss模型|场景模型|环境模型|模型包|贴图|纹理|材质包|动作包|动画片段|体素素材|"
    r"概念资产包|概念包|概念图包|概念设定包|概念图|设定图|三视图|转面图|角色设定|怪物设定|武器设定|场景设定)",
    re.IGNORECASE,
)
_GAME_3D_MODELING_HINT_RE = re.compile(
    r"(建模|模型|精美建模|高模|低模|modeling|modeled|modelled|3d art|character art|enemy art|weapon art|"
    r"texture(?: pack)?|material(?: pack)?|concept(?: art| sheet| pack)?|turnaround|orthographic|"
    r"贴图|纹理|材质|概念图|设定图|三视图|转面图|角色设定|怪物设定|武器设定|场景设定|rigging|animation clip|动作包|动画片段)",
    re.IGNORECASE,
)
_GAME_3D_ASSET_SCALE_HINT_RE = re.compile(
    r"(角色|怪物|monster|enemy|boss|武器|枪械|weapon|gun|载具|vehicle|地图|大地图|map|关卡|level|"
    r"场景|环境|environment|biome|npc|道具|prop|armor|盔甲|skin|皮肤)",
    re.IGNORECASE,
)
_GAME_COMMERCIAL_SCALE_RE = re.compile(
    r"(商业级|商业用途|commercial(?:[- ]grade|[- ]quality)?|production[- ]ready|ship[- ]ready|"
    r"高质量|高规格|高保真|精美)",
    re.IGNORECASE,
)
_GAME_ASSET_PIPELINE_NEGATION_RE = re.compile(
    r"(不要(?:额外)?(?:素材|素材包|模型|模型包|贴图|纹理|图片|image(?:gen)?|asset pack)|"
    r"不用(?:额外)?(?:素材|模型|贴图|纹理|图片)|不需要(?:素材|素材包|模型|贴图|纹理|图片)|"
    r"先用(?:占位|placeholder|临时)(?:几何体|模型|素材|贴图)?|"
    r"占位(?:几何体|模型|素材|贴图)|程序化(?:材质|几何体|geometry|mesh)|"
    r"不要额外(?:图片节点|image node)|只要(?:单页|单文件|index\.html)|只做\s*index\.html)",
    re.IGNORECASE,
)
_GAME_ENGINE_AVOID_RE = re.compile(
    r"(不用引擎|不要引擎|无引擎|原生 canvas|纯 canvas|vanilla canvas|no engine|without engine|engine[- ]?free|simple game|简单小游戏|轻量小游戏)",
    re.IGNORECASE,
)
_GAME_EXPLICIT_SINGLE_FILE_RE = re.compile(
    r"(单页(?:即可|就行|就够|版本)?|单页面|single[- ]page|single page|one[- ]page|one page|"
    r"单文件(?:html)?|single html|one html|self-contained index\.html|standalone index\.html|"
    r"只需(?:要)?\s*index\.html|只做\s*index\.html|index\.html\s*(?:即可|就行|就够))",
    re.IGNORECASE,
)
_GAME_SIMPLE_ENGINELESS_RE = re.compile(
    r"(snake|贪吃蛇|tetris|俄罗斯方块|2048|sudoku|数独|minesweeper|扫雷|pong|breakout|打砖块|memory|记忆翻牌|wordle|拼字游戏|one[- ]?screen)",
    re.IGNORECASE,
)
_GAME_2D_ENGINE_HINT_RE = re.compile(
    r"(platformer|平台跳跃|metroidvania|roguelike|肉鸽|bullet hell|弹幕|tower defense|塔防|top[- ]?down|俯视|"
    r"side[- ]?scroll|横版|shmup|shoot'?em up|boss fight|关卡制|tilemap|physics puzzle|物理益智|camera follow|phaser)",
    re.IGNORECASE,
)
_GAME_3D_ENGINE_HINT_RE = re.compile(
    # v7.4.2: was firing on "建模" (modeling) which 2D games also use for
    # sprite/character art design (PvZ goal "建模精致" → mis-routed to 3D
    # engine path → Three.js runtime preloaded → builder produced PvZ-3D).
    # Removed bare 建模/模型 — only count them as 3D when paired with an
    # actual 3D-context word.
    r"(\b3d\b|three\.?js|webgl|webgpu|fps|first[- ]?person|third[- ]?person|"
    r"racing|driving|flight|space sim|shader|voxel|open world|sandbox|survival|"
    r"3d\s*建模|3d\s*模型|三维\s*建模|3d\s*model|polygonal|低多边形|low[- ]?poly|"
    r"第一人称|第三人称|赛车|飞行|太空|着色器|体素|开放世界|沙盒|生存)",
    re.IGNORECASE,
)
# v7.4.2: explicit 2D markers — take precedence over 3D hints when present.
# User saying "2d的植物大战僵尸" or "二维" should always win over auxiliary
# words like "建模/模型" that previously dragged the goal into 3D mode.
_GAME_EXPLICIT_2D_RE = re.compile(
    r"(\b2d\b|2-d|二维|平面|side[- ]?scroll|横版|top[- ]?down|俯视|tile[- ]?map|"
    r"sprite|精灵图|塔防|tower\s*defense|植物大战僵尸|plants?\s*vs\.?\s*zombies|pvz|"
    r"消消乐|match[- ]?3|三消|纸牌|card\s*game|deckbuild|象棋|围棋|跳棋|chess|"
    r"贪吃蛇|snake|tetris|俄罗斯方块|2048|sudoku|数独|扫雷|minesweeper|pong|breakout)",
    re.IGNORECASE,
)
_GAME_EXISTING_PROJECT_RE = re.compile(
    r"(fix|debug|patch|repair|edit|modify|refactor|continue|iterate|optimi[sz]e|update).*(repo|repository|codebase|project|app|site|game|artifact|build|file|index\.html|app\.js|game\.js|styles?\.css)|"
    r"(existing|current|provided)\s+(repo|repository|codebase|project|app|site|game|artifact|build|file)|"
    r"(修复|调试|补丁|编辑|修改|改一下|基于现有|继续基于|继续优化).*(仓库|代码|项目|游戏|页面|文件)|"
    r"(现有|当前|已有)(仓库|代码|项目|游戏|页面|文件)|"
    r"(仓库里|代码里|项目里|文件里)",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────
# §P1-FIX: Topic-aware image library for content-matched imagery
# Each entry maps a regex pattern to curated Unsplash photo IDs that
# are verified to match the topic. This prevents the LLM from inserting
# random photos (e.g., Eiffel Tower on a China travel site).
# ─────────────────────────────────────────────────────────────────

TOPIC_IMAGE_LIBRARY: Dict[str, Dict[str, List[str]]] = {
    "china_travel": {
        "hero": [
            "photo-1508804185872-d7badad00f7d",  # Great Wall panoramic
            "photo-1547981609-4b6bfe67ca0b",  # Shanghai Bund evening
            "photo-1537531383496-f4749b8032cf",  # Li River karst landscape
        ],
        "attractions": [
            "photo-1584266032559-fe24e2906673",  # Forbidden City
            "photo-1599571234909-29ed5d1321d6",  # Temple of Heaven
            "photo-1513415564515-763d91423bdd",  # Terracotta Warriors
            "photo-1590559899731-a382839e5549",  # Potala Palace
        ],
        "cities": [
            "photo-1474181487882-5abf3f0ba6c2",  # Shanghai skyline
            "photo-1591871937573-74dbba515c4c",  # Beijing night scene
            "photo-1545569341-9eb8b30979d9",  # Hong Kong harbor
            "photo-1523731407965-2430cd12f5e4",  # Chengdu panda
        ],
        "nature": [
            "photo-1513002749550-c59d786b8e6c",  # Zhangjiajie mountains
            "photo-1516496636080-14fb876e029d",  # Jiuzhaigou Valley
            "photo-1504214208698-ea1916a2195a",  # Guilin rice terraces
            "photo-1528164344705-47542687000d",  # Yellow Mountains
        ],
        "food": [
            "photo-1562967916-eb82221dfb44",  # Chinese dim sum
            "photo-1585032226651-759b368d7246",  # Hot pot
        ],
    },
    "food_restaurant": {
        "hero": ["photo-1517248135467-4c7edcad34c4", "photo-1555396273-367ea4eb4db5"],
        "dishes": ["photo-1546069901-ba9599a7e63c", "photo-1504674900247-0877df9cc836"],
        "interior": ["photo-1552566626-52f8b828add9", "photo-1414235077428-338989a2e8c0"],
    },
    "tech_saas": {
        "hero": ["photo-1451187580459-43490279c0fa", "photo-1519389950473-47ba0277781c"],
        "team": ["photo-1522071820081-009f0129c71c", "photo-1600880292203-757bb62b4baf"],
        "product": ["photo-1535303311164-664fc9ec6532", "photo-1460925895917-afdab827c52f"],
    },
    "fashion_lifestyle": {
        "hero": ["photo-1558618666-fcd25c85f82e", "photo-1483985988355-763728e1935b"],
        "products": ["photo-1441984904996-e0b6ba687e04", "photo-1445205170230-053b83016050"],
    },
    "education": {
        "hero": ["photo-1523050854058-8df90110c8f1", "photo-1427504494785-3a9ca7044f45"],
        "campus": ["photo-1541339907198-e08756dedf3f", "photo-1562774053-701939374585"],
    },
    "generic": {
        "hero": ["photo-1497366216548-37526070297c", "photo-1497366811353-6870744d04b2"],
        "team": ["photo-1522071820081-009f0129c71c"],
        "features": ["photo-1460925895917-afdab827c52f", "photo-1535303311164-664fc9ec6532"],
    },
}

_TOPIC_PATTERNS: List[tuple] = [
    (re.compile(r"(中国|china|chinese|华夏|东方|大熊猫|长城|故宫|旅游|旅行|景点|目的地)", re.IGNORECASE), "china_travel"),
    (re.compile(r"(food|restaurant|餐厅|美食|菜单|外卖|饮品|咖啡|cafe)", re.IGNORECASE), "food_restaurant"),
    (re.compile(r"(tech|saas|startup|api|platform|ai|cloud|software|产品|平台|科技)", re.IGNORECASE), "tech_saas"),
    (re.compile(r"(fashion|lifestyle|时尚|潮流|品牌|luxury|奢侈|设计师)", re.IGNORECASE), "fashion_lifestyle"),
    (re.compile(r"(education|school|university|学校|教育|课程|培训|学院)", re.IGNORECASE), "education"),
]


def _detect_image_topic(goal: str) -> str:
    """Detect the image topic from the goal text. Returns a key into TOPIC_IMAGE_LIBRARY."""
    text = str(goal or "")
    for pattern, topic_key in _TOPIC_PATTERNS:
        if pattern.search(text):
            return topic_key
    return "generic"


def _topic_image_block(goal: str) -> str:
    """Generate a topic-specific image directive for the builder prompt.
    Uses curated Unsplash photo IDs matched to the goal's subject matter."""
    topic = _detect_image_topic(goal)
    library = TOPIC_IMAGE_LIBRARY.get(topic, TOPIC_IMAGE_LIBRARY["generic"])

    lines = [
        "J. IMAGE STRATEGY (MANDATORY — topic-matched imagery):\n",
        "   VISUAL COMPOSITION PRIORITY ORDER:\n",
        "   1. CSS/SVG FIRST: Use CSS gradients, SVG illustrations, textures, and layered shape compositions as the primary visual language.\n",
        "      Hero sections should use art-directed layered backgrounds with text overlay, not one giant raw stock photo as the sole visual.\n",
    ]

    if topic == "china_travel":
        lines.extend([
            "   2. LOCATION-SPECIFIC TRAVEL RULE:\n",
            "      For homepage / city / nature / attraction routes, prefer user-provided or analyst-verified URLs first.\n",
            "      When the page topic clearly matches one of the curated categories below, you MAY use the corresponding curated photo URLs directly.\n",
        ])
        for category, ids in library.items():
            formatted_ids = ", ".join(
                f"https://images.unsplash.com/{pid}?w=800&q=80" for pid in ids[:3]
            )
            lines.append(f"      {category.upper()}: {formatted_ids}\n")
        lines.extend([
            "      Do NOT invent a remote photo URL for a specific city or landmark just because it looks plausible.\n",
            "      If the route is about a precise place that is not confidently covered by the curated set or analyst/user-provided sources,\n",
            "      ship a premium non-photo composition with location captioning instead of a wrong photo.\n",
        ])
    else:
        lines.append("   2. CURATED PHOTOS: When real photos are needed, use ONLY these topic-matched references or analyst-provided URLs:\n")
        for category, ids in library.items():
            formatted_ids = ", ".join(
                f"https://images.unsplash.com/{pid}?w=800&q=80" for pid in ids[:3]
            )
            lines.append(f"      {category.upper()}: {formatted_ids}\n")

    lines.extend([
        "   3. FALLBACK: If you need more imagery, use CSS/SVG compositions with captions and overlays. NEVER use random picsum.photos or generic stock photo URLs.\n",
        "   4. RELEVANCE RULE: Every <img> src MUST semantically match the site's topic and visible labels.\n",
        f"      For this site (topic: {topic}), images of unrelated subjects (other countries, random objects) are a BLOCKING quality failure.\n",
        "   Each <img> MUST be wrapped by a framed container with overflow:hidden, object-fit:cover, and a designed CSS fallback state.\n",
        "   Important images should include loading='lazy' or loading='eager' intentionally, decoding='async', and referrerpolicy='no-referrer' for remote hosts.\n",
        "   Card media should default to aspect-ratio:16/9; portrait/editorial slots should explicitly declare a different ratio.\n",
        "   NEVER use empty SVG placeholder boxes or 'image-placeholder' divs as final visuals.\n",
        "   5. PAGE VISUAL COVERAGE: every requested page needs one above-the-fold visual anchor plus one supporting visual/composition block.\n",
        "      Premium website routes must not collapse into text-only pages or empty image slots.\n",
    ])

    return "".join(lines)



def _parse_count_token(token: str) -> int:
    value = str(token or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return max(0, int(value))
    if value == "十":
        return 10
    if len(value) == 2 and value[0] == "十" and value[1] in _CHINESE_DIGIT_MAP:
        return 10 + _CHINESE_DIGIT_MAP[value[1]]
    if len(value) == 2 and value[1] == "十" and value[0] in _CHINESE_DIGIT_MAP:
        return _CHINESE_DIGIT_MAP[value[0]] * 10
    if len(value) == 3 and value[1] == "十" and value[0] in _CHINESE_DIGIT_MAP and value[2] in _CHINESE_DIGIT_MAP:
        return _CHINESE_DIGIT_MAP[value[0]] * 10 + _CHINESE_DIGIT_MAP[value[2]]
    return _CHINESE_DIGIT_MAP.get(value, 0)


def requested_page_count(goal: str) -> int:
    text = str(goal or "")
    best = 0
    for match in _PAGE_COUNT_RE.finditer(text):
        best = max(best, _parse_count_token(match.group("count")))
    return max(best, 1) if best else 1


def wants_multi_page(goal: str) -> bool:
    text = str(goal or "")
    return requested_page_count(text) > 1 or bool(_MULTI_PAGE_HINT_RE.search(text))


def delivery_contract(goal: str) -> str:
    count = requested_page_count(goal)
    if wants_multi_page(goal):
        additional_pages = max(count - 1, 1)
        return (
            "\nDELIVER: the user explicitly asked for a multi-page experience. "
            f"Create index.html plus at least {additional_pages} additional linked HTML page(s) or route files as required by the goal. "
            "Do NOT collapse the request into one long single-page landing page. "
            "Keep the main preview entry at /tmp/evermind_output/index.html and write every page with file_ops. "
            "Shared navigation must let reviewer/tester reach every requested page.\n"
        )
    return (
        "\nDELIVER: file_ops write to /tmp/evermind_output/index.html (preferred). "
        "Or return full HTML in ```html block. Call file_ops write IMMEDIATELY for new projects.\n"
    )


def multi_page_contract(goal: str) -> str:
    if not wants_multi_page(goal):
        return ""
    count = requested_page_count(goal)
    page_label = f"{count} distinct pages/routes" if count > 1 else "multiple distinct pages/routes"
    return (
        "MULTI-PAGE CONTRACT:\n"
        f"- The user asked for {page_label}; do NOT compress this into one scrolling page.\n"
        "- Create a real index entry plus the additional linked HTML pages required by the brief.\n"
        "- Save real route files like index.html, brand.html, contact.html under /tmp/evermind_output/; task_*/index.html preview fallbacks do not count as delivery.\n"
        "- Reuse a consistent design system, but ensure each page has real content and purpose.\n"
        "- Navigation links/buttons must actually open the other requested pages so QA can verify them.\n"
    )


def wants_motion_rich_experience(goal: str) -> bool:
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "website":
            return False
    except Exception:
        return False
    return bool(_MOTION_RICH_HINT_RE.search(text))


def requested_output_language(goal: str) -> str:
    text = str(goal or "")
    if not text:
        return ""
    matches = []
    for match in _ENGLISH_OUTPUT_RE.finditer(text):
        matches.append((match.start(), "en"))
    for match in _CHINESE_OUTPUT_RE.finditer(text):
        matches.append((match.start(), "zh"))
    if not matches:
        return ""
    matches.sort(key=lambda item: item[0])
    return matches[-1][1]


def language_contract(goal: str) -> str:
    language = requested_output_language(goal)
    if language == "en":
        return (
            "LANGUAGE CONTRACT:\n"
            "- All visible UI copy, headings, navigation labels, CTA text, and body copy must be in English.\n"
            "- Do not silently fall back to Chinese or mixed-language copy unless the user explicitly asked for bilingual output.\n"
        )
    if language == "zh":
        return (
            "LANGUAGE CONTRACT:\n"
            "- All visible UI copy, headings, navigation labels, CTA text, and body copy must be in Chinese.\n"
            "- Do not silently switch to English unless the user explicitly asked for bilingual output.\n"
        )
    return ""


def wants_generated_assets(goal: str) -> bool:
    """Return whether the goal explicitly needs the generated-asset pipeline."""
    return bool(game_asset_pipeline_mode(goal))


def _implicit_3d_asset_pipeline_intent(text: str, *, is_3d_or_procedural: bool, is_2d_override: bool) -> bool:
    """Infer when a 3D game brief materially benefits from the asset pipeline.

    This keeps generic 3D gameplay requests on the normal builder path, while
    restoring asset nodes for heavier commercial/modeling briefs that mention
    multiple asset domains (characters, monsters, weapons, maps, environments).
    """
    if not is_3d_or_procedural or is_2d_override:
        return False
    modeling_hint = bool(_GAME_3D_MODELING_HINT_RE.search(text))
    commercial_scale = bool(_GAME_COMMERCIAL_SCALE_RE.search(text))
    asset_scale_hits = {
        str(match.group(0) or "").strip().lower()
        for match in _GAME_3D_ASSET_SCALE_HINT_RE.finditer(text)
        if str(match.group(0) or "").strip()
    }
    asset_scale_count = len(asset_scale_hits)
    if modeling_hint and (commercial_scale or asset_scale_count >= 2):
        return True
    if commercial_scale and asset_scale_count >= 3:
        return True
    return False


def game_asset_pipeline_mode(goal: str) -> str:
    """Return the asset-pipeline mode: '', 'image', '2d', or '3d'."""
    text = str(goal or "")
    if not text:
        return ""
    profile = classify(text)
    if profile.task_type != "game":
        return "image" if _NON_GAME_GENERATED_ASSET_RE.search(text) else ""
    if _GAME_ASSET_PIPELINE_NEGATION_RE.search(text):
        return ""

    has_2d_asset_request = bool(_GAME_EXPLICIT_ASSET_PIPELINE_RE.search(text))
    has_3d_asset_request = bool(_GAME_3D_ASSET_PIPELINE_RE.search(text))
    is_3d_or_procedural = bool(_GAME_PROCEDURAL_OR_3D_RE.search(text))
    is_2d_override = bool(_GAME_2D_SPRITE_OVERRIDE_RE.search(text))
    implicit_3d_asset_intent = _implicit_3d_asset_pipeline_intent(
        text,
        is_3d_or_procedural=is_3d_or_procedural,
        is_2d_override=is_2d_override,
    )

    if has_2d_asset_request and (not is_3d_or_procedural or is_2d_override):
        return "2d"
    if has_3d_asset_request or implicit_3d_asset_intent:
        return "3d"
    if has_2d_asset_request:
        return "2d"

    return ""


def game_runtime_mode(goal: str) -> str:
    text = str(goal or "")
    if not text:
        return ""
    if classify(text).task_type != "game":
        return ""
    if _GAME_ENGINE_AVOID_RE.search(text):
        return "none"
    # v7.4.2: explicit 2D markers (the user said "2d", "塔防", "PvZ", etc.)
    # take precedence over 3D hints. Without this, "2d的植物大战僵尸…建模精致"
    # gets routed to 3d_engine because "建模" used to match the 3D regex.
    explicit_2d = bool(_GAME_EXPLICIT_2D_RE.search(text))
    if _GAME_3D_ENGINE_HINT_RE.search(text) and not explicit_2d:
        return "3d_engine"
    if _GAME_SIMPLE_ENGINELESS_RE.search(text) and not _GAME_2D_ENGINE_HINT_RE.search(text):
        return "none"
    if _GAME_2D_ENGINE_HINT_RE.search(text) or explicit_2d:
        return "2d_engine"
    return "none"


def game_runtime_contract(goal: str) -> str:
    mode = game_runtime_mode(goal)
    if not mode:
        return ""
    if mode == "3d_engine":
        mode_summary = "Use a bundled 3D engine path."
        mode_details = (
            "- You MUST use Three.js for this brief. Canvas2D / getContext('2d') is STRICTLY FORBIDDEN. Do NOT attempt fake-3D with Canvas2D.\n"
            "- Ship the runtime locally from the output directory: <script src='./_evermind_runtime/three/three.min.js'></script>\n"
            "  or import * as THREE from './_evermind_runtime/three/three.module.js'. Never depend on a remote CDN as the only execution path.\n"
        )
    elif mode == "2d_engine":
        mode_summary = "Use a bundled 2D engine path only if it materially helps gameplay."
        mode_details = (
            "- Prefer Phaser for camera-heavy 2D games, tilemaps, platformers, bullet hell, or physics-rich arcade gameplay.\n"
            "- Ship the runtime locally from the output directory, e.g. <script src='./_evermind_runtime/phaser/phaser.min.js'></script>.\n"
            "- If you add richer audio with Howler, load it locally from ./_evermind_runtime/howler/howler.min.js instead of a remote CDN.\n"
        )
    else:
        mode_summary = "Stay engine-free."
        mode_details = (
            "- Use Canvas 2D / DOM / Web Audio directly. Do NOT add Phaser, Three.js, or other engines unless the brief explicitly escalates scope.\n"
            "- Keep the export self-contained and robust in preview/offline static serving.\n"
        )
    combat_fairness_details = (
        "- For shooter / TPS combat, startGame/resetGame must give the player a fair opening window: either keep enemy spawn radius safely beyond first attack range or gate enemy damage with a short spawn grace / invulnerability timer.\n"
        "- When enemy spawns are randomized around the player, validate safety with true radial distance (Math.hypot / distanceTo), not only separate Math.abs(x) / Math.abs(z) checks.\n"
        if goal_requires_combat_start_fairness(goal)
        else ""
    )
    runtime_perf_details = (
        "- Runtime stability baseline: clamp renderer pixel ratio (for example `renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5))`) so the shipped game does not overload HiDPI laptops.\n"
        "- Clamp recovered frame delta after tab/background stalls (for example `const dt = Math.min(clock.getDelta(), 0.05)`) so physics, camera, and projectiles do not jump or freeze after focus returns.\n"
        "- Pause or heavily throttle non-essential updates when `document.hidden` changes, and resume cleanly on focus/visibility restore.\n"
        "- Never let menu-time or pre-start loops call `renderer.render(...)` before renderer/scene/camera exist; either boot the loop after init/start or null-guard the render path until runtime state is ready.\n"
        "- Pool or recycle bullets, tracers, hit sparks, shell casings, and other high-churn combat entities; do NOT let projectile / FX arrays grow without bounds.\n"
        "- Cap simultaneous expensive effects (shadow-casting lights, particles, decals, projectiles) and provide a reduced-quality path on weak/mobile/touch devices instead of hitching the whole game.\n"
    )
    return (
        "GAME RUNTIME / ENGINE CONTRACT:\n"
        f"- Runtime mode for this brief: {mode}.\n"
        f"- {mode_summary}\n"
        f"{mode_details}"
        "- Initialize player/camera/weapon/ammo gameplay state before the first HUD update, render() call, or requestAnimationFrame loop.\n"
        "- Any menu-time or pre-start render/HUD code must null-guard optional runtime state (player, camera, ammo, weapon, HUD refs) until startGame/resetGame finishes.\n"
        f"{combat_fairness_details}"
        f"{runtime_perf_details}"
        "- The exported project must run from the generated output folder without the customer installing any engine separately.\n"
    )


def gameplay_foundation_contract(goal: str) -> str:
    text = str(goal or "")
    if not text:
        return ""
    try:
        if classify(text).task_type != "game":
            return ""
    except Exception:
        return ""

    lines = [
        "GAMEPLAY FOUNDATION CONTRACT:\n"
        "- Keep every <script> parseable from the first save. Do NOT leave dangling braces, half-written functions, or prose placeholders inside JavaScript.\n"
        "- JavaScript syntax guard: NEVER emit dotted object-literal member keys such as `position.y: 1.2`, `rotation.z: yaw`, or `weapon.mesh.position.x: 3` inside `{ ... }`.\n"
        "- Build the parent object first, then mutate nested fields on following statements.\n"
        "- Keep combat/runtime arrays bounded: recycle bullets/projectiles/impact FX instead of creating unbounded per-frame garbage.\n"
    ]
    if game_runtime_mode(text) == "3d_engine":
        lines.extend([
            "- Standard TPS handedness baseline: when yaw=0 and the camera sits behind the player, use `forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)).normalize()` and `right = new THREE.Vector3(forward.z, 0, -forward.x).normalize()`.\n",
            "- Keep the default orbit/follow camera behind the player instead of in front of the avatar. A safe baseline is `const offset = new THREE.Vector3(0, 0, -cameraDistance); offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), pitch); offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), yaw);`.\n",
            "- Anti-mirror movement baseline: W = +forward, S = -forward, A = -right, D = +right.\n",
        ])
        if goal_requires_drag_camera_controls(text) or goal_requires_combat_start_fairness(text):
            lines.extend([
                "- For the common orbit-offset TPS camera, keep drag/pointer look on this sign convention unless the brief explicitly asks otherwise:\n"
                "  ```js\n"
                "  const forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)).normalize();\n"
                "  const right = new THREE.Vector3(forward.z, 0, -forward.x).normalize();\n"
                "  const offset = new THREE.Vector3(0, 0, -cameraDistance);\n"
                "  offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), pitch);\n"
                "  offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), yaw);\n"
                "  if (keys.KeyW) move.add(forward);\n"
                "  if (keys.KeyS) move.sub(forward);\n"
                "  if (keys.KeyA) move.sub(right);\n"
                "  if (keys.KeyD) move.add(right);\n"
                "  yaw += deltaX * sensitivity;\n"
                "  pitch -= deltaY * sensitivity;\n"
                "  pitch = THREE.MathUtils.clamp(pitch, -0.75, 1.05);\n"
                "  ```\n",
                "- Screen-space acceptance: drag right yaws right, drag left yaws left, and drag-up pitches the camera upward by default.\n",
            ])
    return "".join(lines)


def gameplay_foundation_summary(goal: str) -> str:
    text = str(goal or "")
    if not text:
        return ""
    try:
        if classify(text).task_type != "game":
            return ""
    except Exception:
        return ""

    summary = (
        "FOUNDATION CONTRACT: keep every gameplay script parseable and never emit dotted object-literal member writes like `position.y: 1.2`; assign nested fields only after the parent object exists. "
    )
    summary += (
        "Clamp frame delta after long background stalls and recycle bullets / high-churn FX so the shipped game stays responsive instead of freezing after a few combat bursts. "
    )
    if game_runtime_mode(text) == "3d_engine":
        summary += (
            "Use standard TPS handedness with `forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)).normalize()` and `right = new THREE.Vector3(forward.z, 0, -forward.x).normalize()`, keep the default orbit camera behind the player with a negative-Z follow offset, then map W/S to +/-forward and A/D to -/+right. Never swap the strafe signs or the mouse orbit signs. "
        )
        if goal_requires_drag_camera_controls(text) or goal_requires_combat_start_fairness(text):
            summary += (
                "For orbit drag look, use `yaw += deltaX * sensitivity` and `pitch -= deltaY * sensitivity`, then clamp pitch so drag-up pitches up by default. "
            )
        if goal_requires_projectile_readability(text):
            summary += (
                "For shooter/TPS slices, keep a visible centered crosshair/reticle in gameplay, spawn shots from a muzzle or forward aim anchor, and show a readable tracer/projectile core plus muzzle/impact feedback. "
            )
    return summary


def goal_needs_premium_3d_model_contract(goal: str) -> bool:
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "game":
            return False
    except Exception:
        return False
    if game_runtime_mode(text) != "3d_engine":
        return False
    if premium_3d_builder_patch_preferred(text):
        return True
    return (
        game_asset_pipeline_mode(text) == "3d"
        or bool(_GAME_COMMERCIAL_SCALE_RE.search(text))
    )


def goal_requires_stage_progression_flow(goal: str) -> bool:
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "game":
            return False
    except Exception:
        return False
    if goal_needs_premium_3d_model_contract(text):
        return True
    return bool(re.search(
        r"(关卡|阶段|波次|通过页面|通关|结算|胜利|victory|mission complete|level complete|"
        r"stage clear|pass page|boss phase|任务完成)",
        text,
        re.IGNORECASE,
    ))


def goal_requires_drag_camera_controls(goal: str) -> bool:
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "game":
            return False
    except Exception:
        return False
    return bool(re.search(
        r"(长按.*(?:屏幕|鼠标|右键).*(?:拉动|拖动|滑动|转动|旋转).*(?:视角|镜头)|"
        r"(?:鼠标|屏幕|右键).{0,10}长按.{0,20}(?:拉动|拖动|滑动|转动|旋转).{0,12}(?:视角|镜头)|"
        r"(?:按住|长按).{0,12}(?:鼠标|屏幕|右键).{0,24}(?:拖动|拉动|滑动).{0,12}(?:视角|镜头)|"
        r"拉动转动视角|拖动.*视角|旋转.*视角|mouse drag|drag(?:ging)? .*camera|"
        r"hold .*mouse.*drag|rotate view|mouse[- ]look|pointer[- ]drag)",
        text,
        re.IGNORECASE,
    ))


def goal_requires_combat_start_fairness(goal: str) -> bool:
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "game":
            return False
    except Exception:
        return False
    if game_runtime_mode(text) != "3d_engine":
        return False
    if goal_needs_premium_3d_model_contract(text):
        return True
    return bool(re.search(
        r"(射击|shooter|\bfps\b|\btps\b|third[- ]?person|first[- ]?person|枪|枪械|武器|子弹|bullet|projectile|"
        r"enemy|monster|怪物|战斗|combat)",
        text,
        re.IGNORECASE,
    ))


def goal_requires_projectile_readability(goal: str) -> bool:
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "game":
            return False
    except Exception:
        return False
    if re.search(
        r"(射击|shooter|\bfps\b|\btps\b|枪|枪械|武器|火器|bullet|projectile|tracer|ammo|"
        r"子弹|弹道|弹痕|曳光|准心|十字准星|reticle|crosshair|aim|瞄准|开火|fire mode|burst|semi[- ]?auto|auto fire)",
        text,
        re.IGNORECASE,
    ):
        return True
    has_perspective = bool(re.search(
        r"(third[- ]?person|first[- ]?person|第三人称|第一人称|\btps\b|\bfps\b)",
        text,
        re.IGNORECASE,
    ))
    has_combat_payload = bool(re.search(
        r"(weapon|gun|enemy|monster|combat|battle|attack|枪械|武器|怪物|敌人|战斗|攻击)",
        text,
        re.IGNORECASE,
    ))
    if has_perspective and has_combat_payload:
        return True
    return False


def game_3d_modeling_block(goal: str) -> str:
    if game_runtime_mode(goal) != "3d_engine":
        return ""
    if goal_needs_premium_3d_model_contract(goal):
        return (
            "     Use THREE.Mesh / THREE.Group with authored-looking geometry; do NOT treat primitive meshes as the final shipped look.\n"
            "     Premium 3D hero asset rule: the visible player, main enemy, and primary weapon must each include at least one silhouette-defining non-primitive geometry family such as THREE.Shape/ExtrudeGeometry,\n"
            "     LatheGeometry, TubeGeometry, PolyhedronGeometry, custom BufferGeometry, or imported asset scene nodes.\n"
            "     Those non-primitive geometries must appear in the player/enemy/weapon construction paths themselves; using them only on arena props, helper FX, or unrelated scenery does NOT satisfy the contract.\n"
            "     A single token non-primitive detail on an otherwise Box/Sphere/Cylinder/Capsule-dominated hero silhouette still reads as placeholder-grade; the main torso/core/receiver mass must also be authored.\n"
            "     Primitive-only Box/Cone/Cylinder/Sphere/Torus/Capsule stacks still fail quality even when grouped; keep those shapes for tiny pickups, debris, or hidden collision helpers only.\n"
        )
    return (
        "     Use THREE.Mesh / THREE.Group with geometry that matches the role of each object.\n"
        "     Primitive Box/Sphere/Cylinder/Cone/Capsule meshes are acceptable for greybox props, small pickups, hidden collision helpers, or rapid prototypes, but they are NOT a blanket requirement for every visible object.\n"
        "     When the brief asks for stronger presentation, upgrade the visible player, enemy, and hero weapon into authored multi-part groups instead of lone primitive silhouettes.\n"
    )


def game_explicit_single_file_delivery(goal: str) -> bool:
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "game":
            return False
    except Exception:
        return False
    if wants_multi_page(text):
        return False
    return bool(_GAME_EXPLICIT_SINGLE_FILE_RE.search(text))


def game_existing_project_request(goal: str) -> bool:
    """Return whether the brief is about patching an existing game/codebase."""
    text = str(goal or "")
    if not text:
        return False
    lower = text.lower()
    if _GAME_EXISTING_PROJECT_RE.search(text):
        return True
    path_markers = (
        "src/",
        "app/",
        "backend/",
        "frontend/",
        "public/",
        "components/",
        "pages/",
        "scripts/",
    )
    return any(marker in lower for marker in path_markers)


def game_direct_text_delivery_mode(goal: str) -> bool:
    """Return whether direct single-file builder delivery is safe for this game brief."""
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "game":
            return False
    except Exception:
        return False
    if wants_multi_page(text):
        return False
    if game_existing_project_request(text):
        return False
    runtime_mode = game_runtime_mode(text)
    asset_mode = game_asset_pipeline_mode(text)
    if runtime_mode == "3d_engine" and (
        asset_mode == "3d" or bool(_GAME_COMMERCIAL_SCALE_RE.search(text))
    ):
        return False
    return True


def premium_3d_builder_patch_preferred(goal: str) -> bool:
    """Return whether a large premium 3D builder brief should prefer file-based repair over direct text."""
    text = str(goal or "")
    if not text:
        return False
    try:
        if classify(text).task_type != "game":
            return False
    except Exception:
        return False
    if wants_multi_page(text):
        return False
    if game_existing_project_request(text):
        return False
    if game_runtime_mode(text) != "3d_engine":
        return False
    asset_mode = game_asset_pipeline_mode(text)
    if asset_mode == "3d" or bool(_GAME_COMMERCIAL_SCALE_RE.search(text)):
        return True
    premium_visual_markers = bool(re.search(
        r"(精美建模|高质量建模|商业级|commercial(?:[- ]grade|[- ]quality)?|production[- ]ready|premium|"
        r"高质量|精致|人物.*精致|怪物.*精致|建模必须|hero asset|authored model)",
        text,
        re.IGNORECASE,
    ))
    large_combat_scope = bool(re.search(
        r"(射击|shooter|\bfps\b|\btps\b|third[- ]?person|第三人称|怪物|monster|枪|枪械|weapon|"
        r"大地图|open world|arena|关卡|stage|wave|boss|level|敌人|enemy)",
        text,
        re.IGNORECASE,
    ))
    return premium_visual_markers and large_combat_scope


def premium_3d_builder_direct_text_first_pass(goal: str) -> bool:
    """Allow greenfield premium 3D single-page games to ship via direct text first."""
    text = str(goal or "")
    if not text:
        return False
    if not premium_3d_builder_patch_preferred(text):
        return False
    if wants_multi_page(text):
        return False
    if game_existing_project_request(text):
        return False
    return True


def motion_contract(goal: str) -> str:
    if not wants_motion_rich_experience(goal):
        return ""
    multi_page = wants_multi_page(goal)
    transition_line = (
        "- For multi-page sites, include real page-to-page transition treatment or continuity choreography instead of hard cuts.\n"
        if multi_page else
        ""
    )
    return (
        "MOTION CONTRACT:\n"
        "- Build a credible motion system, not just basic button hover states.\n"
        "- The hero or focal object must have purposeful movement, transformation, or scroll-linked behavior when the brief asks for premium motion.\n"
        "- Use layered reveal choreography, easing discipline, and reduced-motion fallbacks.\n"
        f"{transition_line}"
    )


def suggested_multi_page_route_filenames(goal: str) -> List[str]:
    text = str(goal or "")
    if not text:
        return []
    try:
        profile = classify(text)
    except Exception:
        return []
    if profile.task_type != "website" or not wants_multi_page(text):
        return []

    suggestions: List[str] = []

    def _extend(items: List[str]) -> None:
        for item in items:
            name = str(item or "").strip().lower()
            if not name.endswith(".html"):
                continue
            if name == "index.html":
                continue
            if name not in suggestions:
                suggestions.append(name)

    if _TRAVEL_SITE_HINT_RE.search(text):
        _extend([
            "attractions.html",
            "cities.html",
            "nature.html",
            "coast.html",
            "itineraries.html",
            "experiences.html",
            "planning.html",
            "gallery.html",
            "faq.html",
            "contact.html",
            "about.html",
        ])

    return suggestions


# ─────────────────────────────────────────────────────────────────
# Keyword patterns for classification
# ─────────────────────────────────────────────────────────────────

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("game", re.compile(
        # v7.3.3 audit fix MAJOR-3+4: tighten "boss" / "monster" / "score" /
        # "player" / "health" so they only fire in clear game context. Plain
        # "boss site" / "monster.com" / "health score" should NOT classify
        # as game. Strong game-shape words (游戏/手游/gameplay/塔防/关卡/...)
        # remain bare. Ambiguous words now require a co-occurring game cue.
        # v7.3.4 audit MINOR-5: replace `\bgame\b` with a non-letter boundary
        # so "videogame" / "minigame" / "rpg-game" also match. Pure `\b` in
        # Python (UNICODE) treats letters as word chars, so it doesn't fire
        # between two ascii letters.
        r"(游戏|手游|网游|端游|gameplay|(?:^|[\s_\-])game(?=\W|$)|videogame|minigame|pixel\s*art|像素游戏|弹球|贪吃蛇|snake\s*game|tetris|俄罗斯方块|打飞机|射击游戏|"
        r"platformer|跑酷|flappy|pong|breakout|chess|棋|card\s*game|纸牌游戏|\brpg\b|"
        r"arcade|迷宫|maze\s*game|puzzle\s*game|益智游戏|打砖块|消消乐|match-3|tower\s*defense|塔防|"
        # v7.3.2 expanded — these stay broad (already game-distinct)
        r"植物大战僵尸|pvz|plant.{0,4}zombie|僵尸|zombie\s*game|boss\s*(?:fight|battle|战|关|stage|hp)|boss战|"
        r"关卡|level\s*design|level\s*select|玩法|spawn|enemies|血量|"
        r"角色扮演|动作游戏|策略游戏|休闲游戏|横版|roguelike|metroidvania|\bfps\b\s*game|\btps\b\s*game|moba|"
        # ambiguous words gated behind "(.*game|游戏)" — won't false-positive
        r"(?:monster|怪物|player|hp|health|score|积分|leaderboard).{0,40}(?:game|游戏|gameplay|关卡|塔防|fight|spawn))",
        re.IGNORECASE,
    )),
    ("dashboard", re.compile(
        r"(仪表盘|dashboard|后台|admin|管理面板|管理系统|control panel|analytics|"
        r"数据面板|data panel|monitor|监控|CRM|ERP|报表|stock tracker|"
        r"运营平台|管理后台|admin panel|overview page|统计|statistics)",
        re.IGNORECASE,
    )),
    ("tool", re.compile(
        r"(工具|tool|计算器|calculator|转换器|converter|编辑器|editor|"
        r"生成器|generator|format|格式化|密码|password|todo|待办|"
        r"记事本|notepad|markdown|json|timer|计时|countdown|倒计时|"
        r"stopwatch|秒表|圈数|lap\s*time|拼图|puzzle|"
        r"random\s*pick|随机|抽签|抽奖|"
        r"color picker|取色|unit convert|翻译|translator|encoder|decoder|"
        r"clipboard|剪贴板|note\s*taking|笔记|周转|tip\s*calc|"
        r"BMI|身高体重|temperature\s*conv|温度换算|currency\s*conv|汇率|"
        r"checklist|清单|memo|备忘|reminder|提醒)",
        re.IGNORECASE,
    )),
    ("presentation", re.compile(
        r"(PPT|slides|幻灯片|演示|presentation|keynote|slideshow|"
        r"展示页|pitch deck|报告页面|annual report|汇报)",
        re.IGNORECASE,
    )),
    ("creative", re.compile(
        r"(动画|animation|canvas art|创意|creative|3D|three\.?js|"
        r"generative|生成艺术|粒子|particle|shader|WebGL|svg art|"
        r"音乐可视化|music visual|interactive art|交互艺术|"
        r"loading animation|加载动画|clock|时钟|firework|烟花)",
        re.IGNORECASE,
    )),
    # website is the default fallback — broadest patterns
    ("website", re.compile(
        r"(网站|website|网页|web page|官网|landing|着陆页|商城|电商|"
        r"e-commerce|shop|store|blog|博客|portfolio|作品集|company|"
        r"企业|brand|品牌|restaurant|餐厅|hotel|酒店|登录|login|"
        r"signup|注册|首页|homepage|产品页|product|服务|service)",
        re.IGNORECASE,
    )),
]

# ─────────────────────────────────────────────────────────────────
# Specialized profiles
# ─────────────────────────────────────────────────────────────────

_COMMON_RULES = (
    "RULES: Build exactly the artifact shape the user asked for. "
    "If the user asked for a multi-page / multi-route site, create multiple linked HTML pages; otherwise create a single self-contained index.html. "
    "Use CSS vars, responsive @media rules, and deliberate typography choices that fit the product. "
    "Prefer inline SVG, CSS illustration, or bespoke visual treatment over generic icon shortcuts. "
    "Implement as much code as the task actually needs; do not force a tiny low-quality output. "
    "Start <!DOCTYPE html>, end </html>. "
    "NEVER use emoji characters as UI icons, bullet decorations, status markers, or illustrations inside generated pages. "
    "Use inline SVG, CSS shapes, or typography instead. "
    "Before finishing, self-check for blank sections, placeholder copy, broken interactions, and weak visual polish.\n"
    "OUTPUT EFFICIENCY: Keep planning or reasoning text between file_ops calls to ≤40 words. "
    "Focus your token budget on actual code output, not narration.\n"
    "REPORT OUTCOMES FAITHFULLY: If your file_ops write was rejected by a quality gate, "
    "report the rejection honestly and fix the specific issues. Do NOT claim you wrote a "
    "complete page when the write was blocked. Never characterize incomplete or broken work as done.\n"
)


PROFILES: Dict[str, TaskProfile] = {

    # ─── Website ───────────────────────────────────────────────
    "website": TaskProfile(
        task_type="website",
        role="You are a senior product web designer and frontend engineer.",
        design_system=(
            "DESIGN SYSTEM:\n"
            "A. Color palette — choose by content:\n"
            "   Define a multi-surface palette, not a single flat background.\n"
            "   Every page needs at least: base background, elevated surface/cards, and one accent surface or atmospheric glow.\n"
            "   Avoid pure black-only or pure white-only pages unless the brief explicitly demands brutal minimalism.\n"
            "   The root page canvas must NOT default to pure #000/#111 or pure #fff without tinted secondary surfaces and accent color.\n"
            "   Use 2-4 coordinated tones across body, cards, nav, footer, and CTA states so the site never reads as a flat monochrome slab.\n"
            "   Define ALL colors as CSS variables.\n"
            "B. Typography: use a deliberate local/system stack that fits the task. No remote font CDN is required by default.\n"
            "   Prefer premium serif + sans pairings for editorial/travel/luxury pages; use stronger CJK-friendly stacks for Chinese output.\n"
            "C. 8px spacing scale; Cards: border-radius:16px + depth shadow\n"
            "D. Sticky glassmorphism header (backdrop-filter:blur(20px))\n"
            "E. Hero: gradient text (background-clip:text) + glowing CTA\n"
            "F. Buttons: padding:14px 32px, border-radius:12px, hover:translateY(-2px)\n"
            "G. Feature grid: CSS Grid auto-fit minmax(280px,1fr)\n"
            "H. Animations: fadeUp + slideIn with stagger delays\n"
            "I. Motion and atmosphere should default to native CSS/JS (IntersectionObserver, transforms, gradients, masks, scroll progress).\n"
            "   Only load external libraries when analyst notes or loaded skills explicitly require them and you will actively use them.\n"
            "{{TOPIC_IMAGE_BLOCK}}\n"
            "J. IMAGE STYLING (MANDATORY — prevents layout-breaking oversized images):\n"
            "   Global rule: img { max-width:100%; height:auto; display:block; border-radius:12px }\n"
            "   Hero/banner images: wrap in a container with overflow:hidden; height:60vh; max-height:600px\n"
            "     The <img> inside uses width:100%; height:100%; object-fit:cover; object-position:center\n"
            "   Card/feature images: width:100%; aspect-ratio:16/9; object-fit:cover\n"
            "   Avatar/thumbnail images: width:48px; height:48px; border-radius:50%; object-fit:cover\n"
            "   Editorial split media should not exceed about 45vw on desktop unless it is the hero or a deliberate showcase block\n"
            "   Remote image sizing: cards use ?w=800&h=500&fit=crop, heroes use ?w=1600&h=900&fit=crop\n"
            "   NEVER output a bare <img> without wrapper div having overflow:hidden\n"
            "   NEVER use ?w=1920 or larger — use ?w=1600 max for heroes, ?w=800 for cards\n"
            "K. MULTI-PAGE CONSISTENCY:\n"
            "   All pages within a multi-page site MUST reuse identical nav and footer HTML structure,\n"
            "   class names, and link lists. Do NOT create different nav patterns per page.\n"
            "   If the page count is high, shorten labels or compress spacing instead of letting desktop nav wrap messily.\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "<header> sticky nav + brand + 3 links + CTA button\n"
            "<main> hero (with background photo or layered visuals) → trust badges → feature grid (with card images) → showcase → testimonials (with avatar photos) → CTA strip\n"
            "<footer> 2-4 columns + copyright\n"
            "At least 6 visible content blocks.\n"
            "IMAGE REQUIREMENTS: hero section must have a real photo or layered image composition; "
            "feature/showcase cards must include reliable media or designed compositions; "
            "testimonials only need avatar images when testimonials are truly part of the design. "
            "Do not force extra photos into the page just to satisfy a quota.\n"
            "PAGE VISUAL COVERAGE: every requested page must include one meaningful above-the-fold visual anchor and one supporting media/composition block. "
            "Premium website routes must not ship as text-only pages.\n"
        ),
        quality=(
            "Must look like a premium landing page by a pro designer. Not a student project. "
            "Acceptance criteria: (1) hero and key cards have reliable media or designed compositions — no blank placeholders, "
            "(2) ALL images must semantically match the site's topic — REJECT if images show unrelated locations/subjects, "
            "(3) Background treatment has layered surface rhythm instead of a flat black-or-white slab, "
            "(4) Consistent nav/footer across all pages with identical class names, "
            "(5) ALL generated pages must be reachable from homepage navigation — no orphan pages, "
            "(6) Smooth but restrained motion on important sections, "
            "(7) Mobile-responsive layout with no overflow or nav wrapping failure, "
            "(8) No broken links between pages or oversized awkward images, "
            "(9) External libraries are loaded only when prescribed and actually used, "
            "(10) Every premium page keeps a clear visual anchor above the fold instead of collapsing into text-only content."
        ),
        analyst_hint=(
            "Visit 2-3 high-quality reference sites related to the goal. "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Extract: color scheme, layout pattern, typography, key features. Deliver a SHORT design brief. "
            "Try at least 3 different sites to ensure sufficient reference material."
        ),
        tester_hint=(
            "Step1: file-check (index.html exists); "
            "Step2: MUST USE browser tool to navigate to http://127.0.0.1:8765/preview/ and take full-page screenshot; "
            "Step3: MUST USE browser snapshot to inspect visible links/buttons/forms before interacting; "
            "Step4: MUST USE browser tool to scroll down 500px and take another screenshot; "
            "Step5: click at least one real interactive element and MUST verify changed state with wait_for or a second snapshot; "
            "Step6: a PASS verdict is invalid without post-action verification evidence; "
            "Step7: FAIL if browser diagnostics show runtime errors; give PASS/FAIL with concrete visual assessment."
        ),
    ),

    # ─── Game ──────────────────────────────────────────────────
    "game": TaskProfile(
        task_type="game",
        role="You are a senior game developer specializing in browser-based HTML5 games.",
        design_system=(
            "GAME DESIGN SYSTEM:\n"
            "{{GAME_RUNTIME_BLOCK}}"
            "A. RENDERING ENGINE SELECTION (determined by the runtime contract above):\n"
            "   -If runtime mode is 3d_engine: You MUST use Three.js. Canvas2D getContext('2d') is FORBIDDEN.\n"
            "     Load Three.js locally: <script src='./_evermind_runtime/three/three.min.js'></script>\n"
            "     MANDATORY minimum scaffolding for 3D games:\n"
            "       const scene = new THREE.Scene();\n"
            "       const camera = new THREE.PerspectiveCamera(75, window.innerWidth/window.innerHeight, 0.1, 1000);\n"
            "       const renderer = new THREE.WebGLRenderer({antialias: true});\n"
            "       renderer.setSize(window.innerWidth, window.innerHeight);\n"
            "       document.body.appendChild(renderer.domElement);\n"
            "       function animate() { requestAnimationFrame(animate); renderer.render(scene, camera); }\n"
            "       animate();\n"
            "{{GAME_3D_MODELING_BLOCK}}"
            "     Use THREE.DirectionalLight + THREE.AmbientLight for lighting.\n"
            "     NEVER use getContext('2d') or Canvas2D drawing calls when the runtime mode is 3d_engine.\n"
            "   -If runtime mode is none or 2d_engine: Use <canvas> for rendering (2D context) OR pure CSS/DOM.\n"
            "B. Implement a proper game loop: requestAnimationFrame with delta time\n"
            "C. State machine: MENU → PLAYING → PAUSED → GAME_OVER\n"
            "D. Keyboard/touch input handling with event listeners on document (NOT canvas)\n"
            "E. Score system with visual HUD overlay\n"
            "F. Collision detection (AABB or distance-based)\n"
            "G. Particle effects for impacts/explosions/scoring\n"
            "H. Sound: use Web Audio API oscillator for retro SFX (no external files)\n"
            "   OR use local Howler for richer audio: <script src='./_evermind_runtime/howler/howler.min.js'></script>\n"
            "I. Color palette: use a cohesive game palette (e.g. pico-8 inspired)\n"
            "J. Pixel-perfect rendering: image-rendering: pixelated for retro; smooth for modern\n"
            "K. Start screen with title + clickable 'Start Game' button\n"
            "   IMPORTANT: Start button MUST work via mouse click, not only keyboard.\n"
            "   Bind the click in script or explicitly expose the handler before the UI references it.\n"
            "   The game may run inside an iframe where keyboard focus requires a click first.\n"
            "L. Game over screen with score + high score (localStorage) + restart button (clickable)\n"
            "M. Keyboard listeners MUST be on document.addEventListener('keydown', ...) not on canvas\n"
            "N. Auto-focus: when game starts, call canvas.focus() and add tabindex='0' to canvas\n"
            "O. If custom art is required but no external asset generator is attached, create high-quality SVG or pixel placeholders\n"
            "   with a clear asset manifest so imagegen / spritesheet nodes can replace them later without re-architecting the game\n"
            "P. Free game assets (CC0, no attribution needed):\n"
            "     Kenney.nl assets: https://kenney.nl/assets (2D sprites, 3D models, UI, audio)\n"
            "     Use direct PNG/SVG URLs: https://kenney.nl/media/pages/assets/...\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "- Full-viewport canvas or game container (no scroll)\n"
            "- HUD overlay: score (top-left), lives/health (top-right)\n"
            "- Start menu: centered title + subtitle + start button\n"
            "- Game over: fade overlay + final score + play again\n"
            "- Mobile: add on-screen touch controls if applicable\n"
        ),
        quality=(
            "Must feel like a polished indie browser game. Smooth 60fps animation. "
            "Responsive controls. Visual feedback on every action. No jank."
        ),
        analyst_hint=(
            "Research implementation-grade game references. Prioritize GitHub repositories, "
            "technical tutorials, devlogs, postmortems, collision/game-loop writeups, "
            "level-design breakdowns, and official docs for browser game techniques. "
            "You may inspect showcase pages for visual direction, but DO NOT spend time playing "
            "online browser games or treating gameplay as the main research method. "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Summarize mechanics, rendering patterns, controls, asset strategy, and production risks."
        ),
        tester_hint=(
            "Step1: file-check /tmp/evermind_output/ for HTML game files; "
            "Step2: browser navigate to http://127.0.0.1:8765/preview/, take screenshot of start screen; "
            "Step3: browser snapshot to find start/play controls and HUD; "
            "Step4: check browser console for JS errors; "
            "Step5: CLICK the start/play button to begin the game; "
            "Step6: TEST CONTROLS — use press_sequence with Arrow keys/WASD/Space for at least 15 seconds; "
            "Step7: MUST verify changed state hash or visible HUD/score/player movement after gameplay input; "
            "Step8: VERIFY GAMEPLAY — does the player move? Do enemies/obstacles appear? Does scoring work? "
            "Take a screenshot MID-GAMEPLAY showing active gameplay (not the start screen); "
            "Step9: Try to trigger game over and check if game over screen appears; "
            "Step10: PASS only if game is ACTUALLY PLAYABLE — player can move, interact, and game responds to input. "
            "FAIL if: game doesn't start, controls don't work, no gameplay visible, state never changes, or JS errors prevent play."
        ),
    ),

    # ─── Dashboard ─────────────────────────────────────────────
    "dashboard": TaskProfile(
        task_type="dashboard",
        role="You are a senior product designer specializing in data dashboards and admin panels.",
        design_system=(
            "DASHBOARD DESIGN SYSTEM:\n"
            "A. Dark sidebar (240px) + light/dark main content area\n"
            "B. Color: dark sidebar (#1e1e2e), content bg (#f8f9fa or #0f0f1a)\n"
            "   Status colors: green=#22c55e, yellow=#eab308, red=#ef4444, blue=#3b82f6\n"
            "C. Grid layout: CSS Grid for card arrangement; gap:16px\n"
            "D. Stat cards: large number + label + trend indicator (↑/↓ with color)\n"
            "E. Charts: use pure CSS/SVG bar/line/donut charts (no external libs)\n"
            "F. Tables: striped rows, sticky header, hover highlight, compact spacing\n"
            "G. Navigation: sidebar with icon + label, active state highlight\n"
            "H. Top bar: search input + notification bell + user avatar\n"
            "I. Use realistic mock data (not lorem ipsum)\n"
            "J. Typography: Inter; sidebar items 13px, card values 28px bold\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "- Sidebar: logo + nav links (Overview, Analytics, Users, Settings) + collapse\n"
            "- Top bar: page title + search + notifications + profile\n"
            "- Main: 4 stat cards (row) → line/bar chart → data table\n"
            "- Use realistic numbers and labels\n"
        ),
        quality=(
            "Must look like a professional SaaS admin panel (Stripe/Vercel quality). "
            "Clean data hierarchy. Scannable at a glance. Pixel-perfect alignment."
        ),
        analyst_hint=(
            "Visit 2-3 premium dashboard examples (real SaaS products, Dribbble, or admin templates). "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Note layout pattern, card styles, chart types. SHORT summary with concrete examples."
        ),
        tester_hint=(
            "Step1: file-check; "
            "Step2: browser navigate, screenshot full layout; "
            "Step3: browser snapshot to inspect tabs/filters/buttons; "
            "Step4: click at least one filter/tab/control and MUST verify visible state changes with wait_for or a second snapshot; "
            "Step5: a PASS verdict is invalid without post-action verification evidence; "
            "Step6: check sidebar + cards + table render correctly; "
            "Step7: FAIL if browser diagnostics show runtime errors; PASS/FAIL with visual assessment."
        ),
    ),

    # ─── Tool / Utility ────────────────────────────────────────
    "tool": TaskProfile(
        task_type="tool",
        role="You are a senior full-stack engineer building polished web utilities.",
        design_system=(
            "TOOL DESIGN SYSTEM:\n"
            "A. Clean, minimal UI focused on the core function\n"
            "B. Large, clear input areas with proper labels and placeholders\n"
            "C. Instant feedback — output updates live as user types (no page reload)\n"
            "D. Color: neutral bg (#f5f5f5 or #1a1a2e dark), accent for interactive elements\n"
            "E. Copy-to-clipboard buttons on outputs (with checkmark feedback animation)\n"
            "F. Input validation with clear error states (red border + message)\n"
            "G. Keyboard shortcuts where applicable\n"
            "H. Responsive: works on mobile (min-width:320px)\n"
            "I. Clean typography: Inter; input text 14px, labels 12px\n"
            "J. Smooth transitions on all state changes (0.2s ease)\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "- Header: tool name + brief description (one line)\n"
            "- Main: input panel (left/top) + output panel (right/bottom)\n"
            "- Options/settings row if needed\n"
            "- Footer: usage tips or keyboard shortcuts legend\n"
        ),
        quality=(
            "Must feel like a polished utility from a developer tools company. "
            "Instant responsiveness. Zero confusion about what to do."
        ),
        analyst_hint=(
            "Visit 2-3 similar tools online (search for alternatives). "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Note UX pattern, input/output layout, key interactions. SHORT summary."
        ),
        tester_hint=(
            "Step1: file-check; "
            "Step2: browser navigate, screenshot; "
            "Step3: check interactive elements are visible; "
            "Step4: PASS/FAIL — does it look functional and polished?"
        ),
    ),

    # ─── Presentation / Slides ──────────────────────────────────
    "presentation": TaskProfile(
        task_type="presentation",
        role="You are a presentation designer building interactive slide decks with PDF export.",
        design_system=(
            "PRESENTATION DESIGN SYSTEM:\n"
            "RECOMMENDED: Use reveal.js for professional slide presentations:\n"
            "  <link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/reveal.js@5.1.0/dist/reveal.css'>\n"
            "  <link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/reveal.js@5.1.0/dist/theme/black.css'>\n"
            "  <script src='https://cdn.jsdelivr.net/npm/reveal.js@5.1.0/dist/reveal.js'></script>\n"
            "  Available themes: black, white, league, beige, night, serif, simple, solarized, moon, dracula\n"
            "  Plugins: highlight (code), markdown, math, notes, search, zoom\n"
            "  Structure: <div class='reveal'><div class='slides'><section>Slide 1</section><section>Slide 2</section></div></div>\n"
            "  Init: Reveal.initialize({ hash: true, transition: 'slide' })\n"
            "  PDF export: append ?print-pdf to URL, then Ctrl+P → Save as PDF\n"
            "\n"
            "ALTERNATIVE (if reveal.js is not suitable): Build custom slides with:\n"
            "  Each slide 100vw×100vh. Nav: arrows+prev/next buttons+dots+F fullscreen. "
            "Transitions: CSS translateX 0.5s. Large headings clamp(2rem,4vw,4rem). "
            "One key message per slide. Slide counter + progress bar top. "
            "Color by topic: dark #0f172a for tech, white for academic, warm for history. "
            "CSS decorations only (gradients, shapes). "
            "MUST: Download PDF button (window.print). "
            "@media print { .slide{break-after:page;height:auto;min-height:100vh} "
            ".nav-controls,.progress-bar,.dots,.pdf-btn{display:none!important} "
            "*{print-color-adjust:exact} }\n"
        ),
        blueprint=(
            "STRUCTURE (8-10 slides): Title\u2192Overview\u2192Background\u2192Content\u00d74\u2192Takeaways\u2192Q&A. "
            "Nav: prev/next + dots + keyboard + PDF button.\n"
        ),
        quality=(
            "Must look like a premium Keynote presentation. "
            "Clean, bold, one-message-per-slide. Smooth transitions. "
            "PDF export must produce clean pages with no UI controls visible."
        ),
        analyst_hint=(
            "Research the topic: find 3-5 key facts, timeline, and structure ideas. "
            "Also visit 1 premium slide design for layout inspiration. Be FAST."
        ),
        tester_hint=(
            "Step1: file-check; "
            "Step2: MUST USE browser tool to navigate to http://127.0.0.1:8765/preview/, screenshot first slide; "
            "Step3: verify navigation buttons and slide dots are visible; "
            "Step4: verify Download PDF button exists; "
            "Step5: PASS/FAIL — is it a complete, navigable, professional presentation?"
        ),
    ),

    # ─── Creative / Art / Animation ─────────────────────────────
    "creative": TaskProfile(
        task_type="creative",
        role="You are a creative coder building stunning interactive visual experiences.",
        design_system=(
            "CREATIVE DESIGN SYSTEM:\n"
            "A. Full-viewport canvas (100vw × 100vh), no scroll, black or gradient bg\n"
            "B. Use <canvas> 2D or CSS animations (choose what fits best)\n"
            "C. requestAnimationFrame loop with smooth delta-time rendering\n"
            "D. Responsive canvas (resize listener, DPI-aware)\n"
            "E. Interaction: mouse/touch influences the visual (parallax, trails, attraction)\n"
            "F. Color: vibrant, artistic palette; HSL for programmatic color generation\n"
            "G. Easing functions for organic movement: ease-in-out-cubic, spring physics\n"
            "H. Performance: limit particle count, use object pooling if needed\n"
            "I. Subtle UI: small credits text + interaction hint, auto-fade after 3s\n"
            "J. Optional: audio reactivity with Web Audio API\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "- Full-viewport container (no visible UI chrome)\n"
            "- Canvas or CSS art fills entire viewport\n"
            "- Subtle instruction text that fades ('Move your mouse...')\n"
            "- Optional: small toggle button for settings/pause\n"
        ),
        quality=(
            "Must look like a CodePen 'Picked Pen' — visually stunning, "
            "smooth, interactive, and surprising. 60fps minimum."
        ),
        analyst_hint=(
            "Visit 2-3 creative coding examples (CodePen, Shadertoy, or similar). "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Note animation technique, color strategy, and interaction patterns. SHORT summary."
        ),
        tester_hint=(
            "Step1: file-check; "
            "Step2: browser navigate, screenshot; "
            "Step3: check animation runs without errors; "
            "Step4: PASS/FAIL — is it visually impressive?"
        ),
    ),
}


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def classify(goal: str) -> TaskProfile:
    """Classify a user goal into a task type and return its profile."""
    text = (goal or "").strip()
    pattern_map = {task_type: pattern for task_type, pattern in _PATTERNS}

    # v6.0 FIX: builder/orchestrator frequently concatenates user goal with
    # harness reference blocks containing words like "GAME/INTERACTIVE", or
    # "runtime loop", or "start screen". Those literal keywords used to win
    # the game-pattern match and turn a "Hello landing page" into a game
    # task — the quality gate then rejected valid HTML for "missing gameplay".
    # Trim everything after the first harness / reference separator and only
    # look at the first 800 chars of the real user intent.
    _separators = (
        "\n=== REFERENCE",
        "\n=== HARNESS",
        "\n--- HARNESS",
        "\nGAME/INTERACTIVE",
        "\n[BUILDER_",
        "\n=== BUILDER",
    )
    lower_text = text.lower()
    cut = len(text)
    for sep in _separators:
        idx = lower_text.find(sep.lower())
        if idx >= 0 and idx < cut:
            cut = idx
    text = text[:cut][:800].strip() or (goal or "").strip()

    # Keep explicit product-shape tasks deterministic. A "website with animation"
    # is still a website and should keep the website delivery contract.
    #
    # v7.2 FIX — strong website-signal pre-empts presentation.
    # Observed run_d9d45558eb79: user goal "类似淘宝的购物网站...还有动画演示等等"
    # was misclassified as presentation/slides because presentation pattern
    # contains the bare word "演示". The single character "演示" should NOT
    # outweigh "网站/购物/电商" which are unambiguous website signals.
    # Apply game/dashboard/tool first (specific shapes), THEN check for
    # strong website signals before falling through to presentation.
    for task_type in ("game", "dashboard", "tool"):
        pattern = pattern_map.get(task_type)
        if pattern and pattern.search(text):
            return PROFILES[task_type]

    # v7.2 strong-website pre-empt: explicit product-shape words mean website,
    # even if the brief mentions presentation/animation as features.
    _strong_website = re.compile(
        r"(网站|website|网页|web\s*page|官网|商城|电商|e[\-\s]?commerce|"
        r"shop(?:ping)?|store|landing|着陆页|portfolio|作品集|"
        r"博客|blog|首页|homepage|产品页|product\s*page|"
        r"company\s*site|企业站|brand\s*site|品牌站|"
        r"餐厅|restaurant|hotel|酒店|home\s*page)",
        re.IGNORECASE,
    )
    if _strong_website.search(text):
        return PROFILES["website"]

    presentation_pattern = pattern_map.get("presentation")
    if presentation_pattern and presentation_pattern.search(text):
        return PROFILES["presentation"]

    website_pattern = pattern_map.get("website")
    if website_pattern and website_pattern.search(text):
        return PROFILES["website"]

    creative_pattern = pattern_map.get("creative")
    if creative_pattern and creative_pattern.search(text):
        return PROFILES["creative"]

    # Default to website
    return PROFILES["website"]


# ─────────────────────────────────────────────────────────────────
# v7.56 — Required Capability Extraction
# ─────────────────────────────────────────────────────────────────
# When the user's brief explicitly demands a specific technical capability
# (WebGL 3D, 2D canvas game, GLSL shader, Web Audio, physics engine, drag-
# and-drop, real-time charts), the builder MUST actually implement it. The
# task_classifier returns a flat task_type (website/game/...) which loses
# this granularity — observed run_d36f804773d1 where "未来科技 3D 网站 +
# WebGL 创意开发者" was classified as plain "website" (because of the v7.2
# `_strong_website` early-return) and the builder shipped 8 plain HTML
# files with ZERO Three.js / canvas / WebGLRenderer code.
#
# Solution: in addition to the existing task_type, scan the goal for
# capability keywords and attach a list of "required_capabilities" that
# downstream nodes (builder system prompt, reviewer brief, patcher) can
# enforce as MUST-IMPLEMENT contracts.
_CAPABILITY_PATTERNS: List[Tuple[str, "re.Pattern[str]", str]] = [
    (
        "webgl_3d",
        re.compile(
            r"(WebGL|three\.?js|3D\s*(网站|website|场景|scene|portfolio|页面|landing|体验|experience)|"
            r"沉浸式|immersive|spatial\s*web|raymarching|"
            r"GLSL|GPU\s*shader|fragment\s*shader|"
            r"apple\s*vision|awwwards.*3D|3D\s*hero)",
            re.IGNORECASE,
        ),
        # Mandatory implementation contract (v7.58 加强 z-index):
        "WebGL/3D 强制实现：(1) 在 HTML <head> 加 `<script type=\"module\">` "
        "或 `<script src=\"https://unpkg.com/three@0.160.0/build/three.min.js\">` "
        "**同步加载 Three.js**（不允许 document.createElement('script') 动态注入 — "
        "异步加载会让首屏看不到 3D + CDN 失败时永远空白）；(2) 必须 "
        "new THREE.WebGLRenderer + new THREE.Scene + new THREE.PerspectiveCamera "
        "+ 至少 1 个 Mesh 持续 animate()；(3) `<canvas>` 元素必现且 CSS 必须是 "
        "`position:fixed; inset:0; width:100%; height:100%; z-index:-1; "
        "pointer-events:none;` **(z-index:-1 否则页面内容会盖住 3D 背景，"
        "用户看到的就是普通网站不是 3D 体验)**；(4) 加 fallback：如果 WebGL "
        "context 创建失败，给 canvas 一个渐变 background 保底视觉；"
        "(5) 物体颜色不能和页面 body 背景同色 — 3D 物体要清晰可辨。",
    ),
    (
        "canvas_2d_game",
        re.compile(
            r"(2D\s*游戏|2D\s*game|canvas\s*game|网页\s*游戏|browser\s*game|html5\s*game|"
            r"塔防|tower\s*defense|RPG|射击\s*游戏|shooter\s*game|平台\s*游戏|platformer|"
            r"接金币|消消乐|match[- ]?3|解谜\s*游戏|puzzle\s*game|益智|"
            r"贪吃蛇|snake\s*game|俄罗斯方块|tetris|马里奥|mario|"
            r"保卫萝卜|2048|flappy|atari|arcade|"
            r"赛车\s*游戏|racing\s*game|跳跃|jump\s*and\s*run)",
            re.IGNORECASE,
        ),
        "2D Canvas 游戏强制实现：必须 `<canvas>` + 2D context + game loop "
        "(requestAnimationFrame) + 玩家输入处理 (keydown/touch/click) + "
        "至少 1 个角色精灵 + 碰撞检测 + 计分/失败/重启状态。不允许仅 DOM 拼接。",
    ),
    (
        "canvas_art",
        re.compile(
            r"(canvas\s*(动画|animation|art|可视化|visualization)|"
            r"粒子\s*系统|particle\s*system|生成艺术|generative\s*art|"
            r"interactive\s*art|交互艺术|流体\s*模拟|fluid\s*sim)",
            re.IGNORECASE,
        ),
        "Canvas 艺术强制实现：必须 `<canvas>` + requestAnimationFrame + "
        "鼠标/触摸交互影响视觉 + ≥100 个粒子或 grid 单元持续动画。",
    ),
    (
        "shader_glsl",
        re.compile(
            r"(shader|GLSL|fragment\s*shader|vertex\s*shader|"
            r"shadertoy|raymarching|sdf|signed\s*distance\s*function)",
            re.IGNORECASE,
        ),
        "Shader 强制实现：必须真 GLSL fragment shader 字符串（含 uniform "
        "time/resolution + main() 输出 fragColor），通过 Three.js ShaderMaterial "
        "或 raw WebGL pipeline。不允许用 CSS filter/SVG 模拟。",
    ),
    (
        "audio_reactive",
        re.compile(
            r"(音频\s*可视化|audio\s*visual|music\s*visual|声音\s*可视化|"
            r"web\s*audio\s*api|audiocontext|fft\s*分析|frequency\s*spectrum)",
            re.IGNORECASE,
        ),
        "Audio 强制实现：必须 Web Audio API (AudioContext + AnalyserNode + "
        "getByteFrequencyData) → 实时驱动可视化（频谱柱 / 波形 / 粒子动作）。"
        "不允许 fake 随机数模拟。",
    ),
    (
        "physics_engine",
        re.compile(
            r"(物理\s*引擎|physics\s*engine|碰撞\s*模拟|matter\.?js|cannon|rapier|ammo\.?js|"
            r"重力\s*模拟|gravity\s*sim|刚体|rigid\s*body|布料|cloth\s*sim|verlet)",
            re.IGNORECASE,
        ),
        "物理强制实现：matter.js / cannon-es / 自实现 Verlet 积分 + 碰撞检测 + "
        "重力 + ≥5 个交互刚体。状态每帧更新，不允许预计算 keyframe。",
    ),
    (
        "drag_drop",
        re.compile(
            r"(拖拽|drag\s*(and|&)?\s*drop|拖动\s*排序|sortable|kanban|"
            r"看板|trello|notion[- ]?like|dnd)",
            re.IGNORECASE,
        ),
        "拖拽强制实现：HTML5 native drag-and-drop API（dragstart/dragover/drop）"
        "或 pointer events 完整链 + 视觉反馈 (drag-ghost, drop-indicator)。"
        "不允许仅 CSS hover。",
    ),
    (
        "real_time_chart",
        re.compile(
            r"(实时\s*图表|real-?time\s*chart|实时\s*数据|live\s*data|"
            r"实时\s*仪表盘|realtime\s*dashboard|stock\s*tracker|股票|crypto\s*price)",
            re.IGNORECASE,
        ),
        "实时图表强制实现：Chart.js / D3 / ECharts / canvas 自绘 + "
        "setInterval/WebSocket/MOCK 数据流（≥1 秒一次） + 图表持续重绘（不能是静态 SVG）。",
    ),
    (
        "video_embed",
        re.compile(
            r"(嵌入\s*视频|插入\s*视频|video\s*embed|video\s*background|"
            r"video\s*hero|背景\s*视频|视频\s*背景|视频\s*hero|"
            r"加\s*视频|有\s*视频|视频\s*插入|<video\b)",
            re.IGNORECASE,
        ),
        "视频强制实现：至少 1 个 `<video>` 元素 + autoplay/muted/loop/playsinline "
        "+ poster fallback。可使用 mp4/webm 远程 URL 或 picsum/unsplash 占位。",
    ),
    (
        "scroll_driven",
        re.compile(
            r"(滚动\s*驱动|scroll[- ]?driven|scrollytelling|滚动\s*动画|"
            r"GSAP\s*ScrollTrigger|scroll\s*timeline|视差\s*滚动|parallax)",
            re.IGNORECASE,
        ),
        "滚动驱动强制实现：IntersectionObserver 或 CSS scroll-timeline 或 GSAP "
        "ScrollTrigger + 至少 3 个 section 在不同滚动位置触发不同动画状态。"
        "纯 CSS hover 不算。",
    ),
]


def extract_required_capabilities(goal: str) -> List[Tuple[str, str]]:
    """v7.56: scan the user goal for capability keywords and return a list
    of (capability_name, mandatory_contract_text) tuples.

    Multiple capabilities can match (e.g. "3D portfolio with audio
    reactive shader" → webgl_3d + shader_glsl + audio_reactive). Builder
    system prompt will inject ALL matching contracts so each must be
    implemented.

    Returns empty list when no specific capability is required (plain
    website/dashboard/tool tasks). Existing builder prompt machinery
    handles those.
    """
    text = (goal or "").strip()
    if not text:
        return []
    # Same harness-block trim as classify() to avoid harness leakage
    _separators = (
        "\n=== REFERENCE", "\n=== HARNESS", "\n--- HARNESS",
        "\nGAME/INTERACTIVE", "\n[BUILDER_", "\n=== BUILDER",
    )
    lower = text.lower()
    cut = len(text)
    for sep in _separators:
        idx = lower.find(sep.lower())
        if idx >= 0 and idx < cut:
            cut = idx
    text = text[:cut][:2000].strip() or (goal or "").strip()

    matched: List[Tuple[str, str]] = []
    for cap_name, pattern, contract in _CAPABILITY_PATTERNS:
        if pattern.search(text):
            matched.append((cap_name, contract))
    return matched


def required_capability_block(goal: str) -> str:
    """v7.56: format extracted capabilities as a builder-system-prompt
    enforcement block. Returns empty string when no capabilities matched.

    Used by builder_system_prompt to append a "MUST IMPLEMENT" section
    so the LLM cannot ship a plain website when the brief explicitly
    asks for 3D / 2D game / shader etc.
    """
    caps = extract_required_capabilities(goal)
    if not caps:
        return ""
    lines = [
        "",
        "=== V7.56 REQUIRED CAPABILITY ENFORCEMENT (mandatory) ===",
        "用户的 brief 明确要求以下技术能力。本次产出必须每项都真实实现，"
        "否则 reviewer 会直接打 0 分并触发 patcher / builder 重做。",
        "",
    ]
    for i, (cap_name, contract) in enumerate(caps, 1):
        lines.append(f"[{i}] {cap_name.upper()}: {contract}")
    lines.append("")
    lines.append("禁止用 \"占位\" 或 \"TODO\" 或 \"略\" 跳过任何一项；")
    lines.append("禁止用 CSS 动画 / SVG 装饰冒充 WebGL/Canvas/Shader 真实现；")
    lines.append("如果你最终交付的产物缺少任意一项能力，本次 build 将被记为失败。")
    lines.append("=== END CAPABILITY ENFORCEMENT ===")
    lines.append("")
    return "\n".join(lines)


def builder_system_prompt(goal: str, *, split_deferred: bool = False):
    """Generate a task-adaptive builder system prompt.

    When *split_deferred* is True, returns ``(core_prompt, deferred_context)``
    where *deferred_context* holds large reference blocks (CSS templates,
    design-system data, topic images) that can be injected into the user
    message instead. This keeps the system prompt small and stable for
    prompt caching — the system prefix no longer changes between different
    CSS templates, reducing wasted input tokens on multi-round tool calls.

    When *split_deferred* is False (default), returns a single string with
    everything inlined, preserving backward compatibility.
    """
    profile = classify(goal)

    # Load pre-built CSS templates
    base_css = _load_template("base.css")
    type_css_map = {
        "website": "website.css",
        "presentation": "presentation.css",
        "game": "game.css",
        "dashboard": "dashboard.css",
        "tool": "website.css",
        "creative": "game.css",
    }
    type_css = _load_template(type_css_map.get(profile.task_type, "website.css"))

    css_block = ""
    if profile.task_type != "game" and (base_css or type_css):
        combined = base_css
        if type_css:
            combined += "\n\n" + type_css
        css_block = (
            "\n=== PRE-BUILT CSS DESIGN SYSTEM (MUST USE) ===\n"
            "Copy this ENTIRE CSS into your <style> tag. You may customize CSS variable values "
            "(colors, sizes) to match the project, but KEEP all the component classes.\n"
            "This saves you from writing CSS from scratch — focus on HTML structure and content.\n\n"
            f"```css\n{combined}\n```\n"
            "=== END CSS ===\n\n"
        )

    delivery_block = delivery_contract(goal)
    multi_page_block = multi_page_contract(goal)
    motion_block = motion_contract(goal)
    language_block = language_contract(goal)
    first_write_contract = ""
    if profile.task_type in {"game", "dashboard", "tool", "presentation", "creative"}:
        first_write_contract = (
            "FIRST WRITE CONTRACT:\n"
            "- Your first successful write must already contain visible <body> content, not only a design-system <style> block.\n"
            "- Keep the first-pass CSS concise and functional; do not spend the whole first pass on tokens, gradients, or long animation systems before the main content exists.\n"
        )
    if profile.task_type == "game":
        first_write_contract += (
            "- For games, the first saved HTML must already render a playable shell: start screen, gameplay viewport/canvas or arena, HUD, keyboard/pointer input bindings, a requestAnimationFrame loop, and at least one visible gameplay entity or stage element.\n"
            "- Include a detectable fail/win/restart path in the saved game shell instead of deferring end-state handling to a later pass.\n"
            "- Keep game UI CSS lightweight on the first pass; prioritize working gameplay shell and visible body content over large decorative style systems.\n"
        )
        if goal_requires_combat_start_fairness(goal):
            first_write_contract += (
                "- For shooter / TPS combat, the first saved shell must already prevent spawn-kill openings: keep enemy spawn safety based on true radial distance and/or add a short spawn grace / invulnerability window so QA can move, aim, and fire before unavoidable damage.\n"
                "- If you ship drag-camera or pointer-lock look on a TPS / shooter brief, default to standard non-inverted vertical look: dragging or mousing upward pitches the camera upward unless the brief explicitly asks for inverted look.\n"
            )
        if goal_requires_stage_progression_flow(goal):
            first_write_contract += (
                "- When the brief asks for stages/clear/pass flow or is a premium 3D combat brief, the first saved shell must already include finite wave/stage progression plus a visible mission-complete / victory / pass screen. Endless survival only is rejected.\n"
            )
        if goal_requires_drag_camera_controls(goal):
            first_write_contract += (
                "- When the brief asks for drag-to-rotate camera control, the first saved shell must already wire real mouse/pointer drag or pointer-lock look that visibly rotates the gameplay camera.\n"
                "- Control sign contract: dragging right must yaw the camera right, dragging left must yaw left, and forward movement must follow the camera-facing forward vector rather than feeling mirrored.\n"
            )

    # §P1-FIX: Replace the {{TOPIC_IMAGE_BLOCK}} placeholder with topic-aware imagery
    design_system_with_images = profile.design_system.replace(
        "{{TOPIC_IMAGE_BLOCK}}", _topic_image_block(goal)
    )
    design_system_with_runtime = design_system_with_images.replace(
        "{{GAME_RUNTIME_BLOCK}}", game_runtime_contract(goal)
    )
    design_system_with_runtime = design_system_with_runtime.replace(
        "{{GAME_3D_MODELING_BLOCK}}", game_3d_modeling_block(goal)
    )
    foundation_block = gameplay_foundation_contract(goal)

    # v7.56 FIX: capability block injected into the
    # ACTUAL builder system prompt return paths (not builder_task_description).
    # Both legacy and split_deferred paths must include it so the LLM cannot
    # ship a plain website when brief explicitly asks for 3D/2D-game/shader/etc.
    builder_capability_block = required_capability_block(goal)

    if not split_deferred:
        # Legacy path: everything in one system prompt string
        return (
            f"{profile.role}\n"
            f"{_COMMON_RULES}"
            f"{css_block}"
            f"{design_system_with_runtime}"
            f"{foundation_block}"
            f"{profile.blueprint}"
            f"{first_write_contract}"
            f"{multi_page_block}"
            f"{motion_block}"
            f"{language_block}"
            f"Quality: {profile.quality}\n"
            f"{delivery_block}"
            f"{builder_capability_block}"
        )

    # V4.6 SPEED: Split into compact system prompt + deferred user context.
    # System prompt: identity + behavioral rules + contracts (stable prefix for caching)
    # Deferred: CSS templates + design system data + topic images (reference material)
    core_prompt = (
        f"{profile.role}\n"
        f"{_COMMON_RULES}"
        f"{profile.blueprint}"
        f"{first_write_contract}"
        f"{multi_page_block}"
        f"{motion_block}"
        f"{language_block}"
        f"Quality: {profile.quality}\n"
        f"{delivery_block}"
        f"{builder_capability_block}"
    )
    deferred_parts = []
    if css_block:
        deferred_parts.append(css_block)
    if design_system_with_runtime:
        deferred_parts.append(design_system_with_runtime)
    if foundation_block:
        deferred_parts.append(foundation_block)
    deferred_context = "\n".join(deferred_parts)

    return (core_prompt, deferred_context)


def builder_task_description(goal: str) -> str:
    """Generate a CONCISE task-adaptive builder task description for the orchestrator.
    NOTE: design_system/blueprint/knowledge are already in the system prompt — do NOT repeat them here.
    """
    profile = classify(goal)
    type_label = {
        "website": "website", "game": "HTML5 game",
        "dashboard": "dashboard/admin panel", "tool": "web utility/tool",
        "presentation": "presentation/slides", "creative": "creative visual experience",
    }.get(profile.task_type, "web application")

    multi_page = wants_multi_page(goal)
    count = requested_page_count(goal)
    motion_line = ""
    if wants_motion_rich_experience(goal):
        motion_line = (
            "Implement a real premium motion system with hero/focal motion, layered section reveals, "
            + (
                "and page-to-page transition treatment. "
                if multi_page else
                "and meaningful transition choreography. "
            )
        )
    if multi_page and count > 1:
        delivery_line = f"Create index.html plus at least {count - 1} additional linked HTML page(s) via file_ops write. "
    elif multi_page:
        delivery_line = "Create index.html plus the additional linked HTML pages required by the brief via file_ops write. "
    else:
        delivery_line = "Save final HTML via file_ops write to /tmp/evermind_output/index.html. "

    language = requested_output_language(goal)
    language_line = ""
    if language == "en":
        language_line = "All UI copy and navigation labels must be in English. "
    elif language == "zh":
        language_line = "All UI copy and navigation labels must be in Chinese. "

    scope_line = (
        f"Build a commercial-grade multi-page {type_label} for: {goal}. "
        if multi_page
        else f"Build a commercial-grade {type_label} for: {goal}. "
    )
    compact_first_save_line = ""
    if profile.task_type in {"game", "dashboard", "tool", "presentation", "creative"}:
        compact_first_save_line = (
            "Ship a compact first-save vertical slice before extra polish: valid HTML skeleton, one working primary flow, "
            "and concise data/config. Avoid huge inline assets, giant map arrays, or base64 blobs in the first pass. "
        )
    if profile.task_type == "game":
        compact_first_save_line += (
            "For games, the first save must already show a visible start screen, gameplay viewport/canvas or arena, HUD, "
            "and at least one enemy/objective/stage element inside <body>; do not spend the whole first pass on CSS alone. "
            "Initialize gameplay state before the first HUD/render tick, and null-guard menu-time player/camera/ammo reads until start/reset completes. "
        )
    game_runtime_line = ""
    if profile.task_type == "game":
        mode = game_runtime_mode(goal)
        if mode == "3d_engine":
            game_runtime_line = (
                "ENGINE CONTRACT: this brief merits a bundled local 3D runtime. Use local Three.js under "
                "./_evermind_runtime/three/three.min.js or ./_evermind_runtime/three/three.module.js; never rely on a remote CDN as the only runtime path. "
            )
        elif mode == "2d_engine":
            game_runtime_line = (
                "ENGINE CONTRACT: this brief may use a bundled local 2D engine when it materially improves gameplay. "
                "If you use Phaser or Howler, load them from ./_evermind_runtime/... local files, not a remote CDN. "
            )
        else:
            game_runtime_line = (
                "ENGINE CONTRACT: keep this game engine-free unless the brief explicitly escalates scope. "
                "Prefer Canvas 2D / DOM / Web Audio over unnecessary frameworks. "
            )
    premium_3d_model_contract = ""
    if profile.task_type == "game" and goal_needs_premium_3d_model_contract(goal):
        premium_3d_model_contract = (
            "PREMIUM 3D MODEL CONTRACT: the visible player, main enemy, and primary weapon must read as authored hero assets, not placeholder geometry. "
            "Each core model must include at least one silhouette-defining non-primitive geometry family such as THREE.Shape/ExtrudeGeometry, "
            "LatheGeometry, TubeGeometry, PolyhedronGeometry, custom BufferGeometry, or imported asset scene nodes. "
            "Those non-primitive geometry calls must appear in the player/enemy/weapon construction paths themselves; scenery-only usage does not satisfy the contract. "
            "One stray non-primitive detail on an otherwise sphere/box/cylinder-dominated silhouette still fails; the main torso/core/receiver mass must also be authored. "
            "Primitive-only Box/Cone/Cylinder/Sphere/Torus/Capsule stacks still fail quality even when grouped. "
            "Use multiple MeshStandardMaterial zones with tuned roughness / metalness / emissive accents for those hero assets. "
        )
    stage_progression_contract = ""
    if profile.task_type == "game" and goal_requires_stage_progression_flow(goal):
        stage_progression_contract = (
            "PROGRESSION CONTRACT: implement finite stage/wave progression and a visible victory / mission-complete / pass screen in the shipped first pass; endless survival only is rejected. "
            "Use explicit progression state such as currentStage/stage/wave/currentWave/maxStages plus concrete advancement logic like nextWave()/nextStage()/stage++/wave++, so the progression is both real and reviewable. "
        )
    drag_camera_contract = ""
    if profile.task_type == "game" and goal_requires_drag_camera_controls(goal):
        drag_camera_contract = (
            "CAMERA CONTROL CONTRACT: during gameplay, mouse/pointer drag or an active pointer-lock look path must visibly rotate the third-person camera; a static follow camera is not enough. "
            "Dragging right must yaw the camera right around the player, dragging left must yaw left, dragging up must pitch the camera upward by default, and forward movement must follow the camera-facing forward vector rather than feeling mirrored. "
        )
    combat_fairness_contract = ""
    if profile.task_type == "game" and goal_requires_combat_start_fairness(goal):
        combat_fairness_contract = (
            "COMBAT FAIRNESS CONTRACT: after Start/Restart, the player must get a fair opening window to move, aim, and fire before unavoidable damage lands. "
            "Keep initial enemy spawn distance comfortably beyond first attack range and/or gate incoming damage with a short spawn grace / invulnerability timer. "
            "If spawn positions are randomized around the player, use true radial distance checks instead of only per-axis Math.abs(x)/Math.abs(z) exclusions. "
            "If you include drag-camera or pointer-lock look, dragging or mousing upward must pitch upward by default unless the brief explicitly requests inverted look. "
        )
    gameplay_foundation_line = gameplay_foundation_summary(goal) if profile.task_type == "game" else ""
    runtime_perf_contract = ""
    if profile.task_type == "game":
        runtime_perf_contract = (
            "RUNTIME PERFORMANCE CONTRACT: cap renderer pixel ratio, clamp large frame deltas after tab/background stalls, pause or throttle non-essential work on visibilitychange, and pool bullets/projectiles/impact FX so the shipped game does not hitch or freeze during combat. "
        )
    shooter_hud_contract = ""
    if profile.task_type == "game" and goal_requires_projectile_readability(goal):
        shooter_hud_contract = (
            "AIM/HUD CONTRACT: keep a visible centered crosshair/reticle during gameplay, keep ammo/weapon state readable in the HUD, and align shot direction to the same aim vector represented by the reticle. "
        )

    if profile.task_type == "game":
        return (
            f"{scope_line}"
            f"{delivery_line}"
            f"{language_line}"
            "Follow the gameplay, UI, and runtime rules from your system prompt. "
            "Treat any upstream planner/analyst notes, loaded skills, reviewer blockers, and acceptance criteria as hard requirements, not optional inspiration. "
            "Do not use emoji glyphs in the generated product; use SVG/CSS alternatives instead. "
            "Make the result materially complete: real menus, real gameplay, real HUD/state feedback, and visible polish. "
            "VISUAL CONTRACT: avoid a flat pure-black/pure-white shell; use layered surfaces, readable depth, and one coherent accent system so menus/HUD/world framing feel intentionally designed. "
            f"{gameplay_foundation_line}"
            f"{runtime_perf_contract}"
            f"{premium_3d_model_contract}"
            f"{stage_progression_contract}"
            f"{drag_camera_contract}"
            f"{combat_fairness_contract}"
            f"{shooter_hud_contract}"
            f"{compact_first_save_line}"
            "FIRST PLAYABLE SLICE CONTRACT: visible start screen/menu -> initialized scene/camera/renderer -> look controls -> movement -> fire/action loop -> at least one enemy/objective/stage element -> HUD/status updates -> fail/win/restart path. "
            "For shooter/TPS briefs, prioritize playable combat and readable camera feel before extra decorative set dressing. "
            f"{game_runtime_line}"
            "Keep styling preview-safe: use inline <style>/<script> plus bundled local runtime files when needed; do not depend on Tailwind CDN or a remote engine CDN as the primary execution path. "
            "For game or asset-heavy tasks, preserve a clean asset manifest / placeholder structure so dedicated asset nodes can upgrade art without rewriting the core logic. "
            "RESOURCE INTEGRITY: do NOT reference local files that do not exist in the output directory. Use bundled local runtime files that really ship with the export, inline SVG/CSS, or a clear replacement manifest instead of invented paths. "
            "DELIVERY CONTRACT: output one complete playable file from <!DOCTYPE html> to </html>; do not output only a patch fragment, CSS shell, or JS tail. "
            "After saving, briefly describe exactly what you built and what quality checks you satisfied."
        )

    # v7.56 NOTE: capability enforcement is injected into
    # the builder SYSTEM PROMPT (builder_system_prompt return paths), not
    # here in the task description. Task description stays short to avoid
    # double-injection and prompt bloat.

    return (
        f"{scope_line}"
        f"{delivery_line}"
        f"{language_line}"
        "Follow the design system and structure rules from your system prompt. "
        "Treat any upstream planner/analyst notes, loaded skills, reviewer blockers, and acceptance criteria as hard requirements, not optional inspiration. "
        "Do not use emoji glyphs in the generated product; use SVG/CSS alternatives instead. "
        "Make the result materially complete: real sections, real content, real interactions, and visible polish. "
        "PALETTE CONTRACT: do NOT default the site to a flat pure-black (#000/#111) or pure-white (#fff) canvas. "
        "Use at least three coordinated surfaces/tones plus an accent/glow so backgrounds, cards, nav, and footer feel intentionally designed. "
        "PAGE VISUAL CONTRACT: every requested page must include at least one meaningful above-the-fold visual anchor and one supporting media/composition block; "
        "do not leave image slots empty or reduce routes to text-only sections. "
        "NAV CONTRACT: if the site has many pages, shorten labels, tighten spacing, or collapse responsively before allowing desktop nav to wrap awkwardly. "
        f"{compact_first_save_line}"
        f"{game_runtime_line}"
        f"{motion_line}"
        "If the brief requests multiple pages/routes, do NOT fake it as one long landing page; every required page must exist and be reachable from navigation. "
        "Keep styling preview-safe: include an inline <style> block or write a local stylesheet file; do not depend on Tailwind CDN or other remote CSS runtimes as the main styling path. "
        "IMAGE SIZING: All <img> must have max-width:100%;height:auto. Hero images use object-fit:cover inside a fixed-height container (60vh). Unsplash URLs: heroes ?w=1600&h=900&fit=crop, cards ?w=800&h=500&fit=crop. Never use ?w=1920 or bare uncontained images. "
        "IMAGE TRUTH RULE: For landmark/location-specific imagery, use only user-provided or analyst-verified URLs. If you cannot verify an exact image, build a premium CSS/SVG composition instead of using a wrong photo. "
        "For game or asset-heavy tasks, preserve a clean asset manifest / placeholder structure so dedicated asset nodes can upgrade art without rewriting the core logic. "
        "RESOURCE INTEGRITY: Do NOT reference local files (images, fonts, JS) that do not exist in the output directory. "
        "Use bundled local runtime files only when they really ship with the export; otherwise use verified remote URLs, inline SVG, or CSS compositions instead of invented fake paths like 'hero-bg.jpg' or 'logo.png'. "
        "SVG SIZING: All inline <svg> elements MUST have explicit width and height attributes (e.g. width='48' height='48'). A viewBox alone is NOT enough — SVG without width/height expands to fill its container and creates oversized shapes. For decorative icons use 24-48px, for feature icons use 48-64px, for hero artwork use responsive CSS (max-width:200px). NEVER leave an SVG without width and height. "
        "After saving, briefly describe exactly what you built and what quality checks you satisfied."
    )


def analyst_description(goal: str) -> str:
    """Generate analyst task description based on task type."""
    profile = classify(goal)
    game_research_rule = ""
    game_control_contract_rule = ""
    language = requested_output_language(goal)
    language_rule = ""
    if language == "en":
        language_rule = (
            "LANGUAGE REQUIREMENT:\n"
            "- The final product must be in English, so handoff labels, page names, sample copy guidance, and reviewer criteria should all assume English UI copy.\n\n"
        )
    elif language == "zh":
        language_rule = (
            "LANGUAGE REQUIREMENT:\n"
            "- The final product must be in Chinese, so handoff labels, page names, sample copy guidance, and reviewer criteria should all assume Chinese UI copy.\n\n"
        )
    if profile.task_type == "game":
        game_research_rule = (
            "GAME RESEARCH OVERRIDE:\n"
            "- Do NOT browse playable web games as your primary workflow\n"
            "- Do NOT get stuck interacting with game portals or gameplay embeds\n"
            "- Prefer GitHub repos, source code, technical articles, tutorials, devlogs, postmortems, and engine/docs pages\n"
            "- Prefer source_fetch first on GitHub/blob/raw files and docs pages so you can hand downstream nodes exact repo/file anchors\n"
            "- source_fetch is Crawl4AI-backed when available: use it first for repo/docs harvesting before falling back to browser-only inspection\n"
            "- If you inspect a reference page, extract mechanics/UI/asset insights quickly and move on\n\n"
        )
        game_control_contract_rule = (
            "GAME CONTROL / CAMERA CONTRACT:\n"
            "- You MUST define the control frame in both world space and screen space so downstream builder cannot mirror controls\n"
            "- Explicitly state what W/A/S/D (or arrows), click/tap, mouse drag, touch drag, and camera yaw/pitch do\n"
            "- Include anti-mirror acceptance checks: with the camera behind the player, W moves away from camera, S moves toward camera, A moves screen-left, D moves screen-right\n"
            "- For orbit/follow camera rigs, call out a behind-player baseline explicitly (for example `offset = new THREE.Vector3(0, 0, -cameraDistance)` before pitch/yaw rotation) so builder does not accidentally place the camera in front of the avatar\n"
            "- For drag-look camera goals, dragging right must turn the view right and push a centered landmark toward the left side of the screen; dragging left must do the opposite\n"
            "- State whether pitch is inverted or not; default is NOT inverted unless the brief says otherwise\n"
            "- Include startup survivability checks: after pressing Start the player must have time to move, aim, and fire before unavoidable damage; define either a spawn grace window or a minimum enemy spawn radius that exceeds attack range\n"
            "- If the game randomizes enemy spawn positions around the player, require true radial distance checks instead of only per-axis Math.abs(x)/Math.abs(z) exclusions\n"
            "- Include runtime stability checks: cap renderer pixel ratio, clamp post-background delta time, pause/throttle on visibilitychange, and require bounded projectile/FX pools instead of unbounded arrays\n"
            "- For premium 3D hero assets, forbid sphere/box/cylinder-dominated player, enemy, or weapon silhouettes with only one token non-primitive accent\n"
            "- Include a permissive asset sourcing plan: licensed libraries first, authored procedural geometry / CSS-SVG fallback second\n\n"
        )
    # §P1-FIX: Inject topic-aware image curation directive for analyst
    topic = _detect_image_topic(goal)
    topic_library = TOPIC_IMAGE_LIBRARY.get(topic, TOPIC_IMAGE_LIBRARY["generic"])
    image_curation_hint = (
        "IMAGE CURATION MANDATE:\n"
        f"- Detected topic: {topic}\n"
        "- You MUST include a <curated_image_library> section in your output\n"
        "- For location-specific / travel / history pages, prefer analyst-verified direct image URLs from trustworthy sources (official tourism media, Wikimedia Commons, museum/park media, or clearly topic-matched editorial sources)\n"
        "- NEVER recommend generic stock photos, picsum.photos, or vague 'search for a photo of X later' placeholders\n"
        "- If exact imagery cannot be verified with high confidence, explicitly instruct builder to use a premium CSS/SVG composition instead of a mismatched photo\n"
        "- Recommend CSS/SVG visual compositions (gradients, patterns, shapes) as the primary fallback visual language\n"
        "- For each page, specify: hero image plan, supporting media plan, background treatment, and whether the slot is photo-backed or composition-backed\n\n"
    )

    return (
        f"{profile.analyst_hint}\n\n"
        "MANDATORY: You MUST search and visit AT LEAST 2 different reference sources. "
        "Use a 3rd source only if the first 2 leave a concrete gap. If a site blocks you (captcha, 403, etc), "
        "skip it immediately and try another URL. Include ALL visited URLs in your report.\n\n"
        "REFERENCE MIX REQUIREMENT:\n"
        "- For implementation-heavy tasks, include at least 1 GitHub/source-code reference when possible\n"
        "- Include at least 1 tutorial / official doc / technical writeup when possible\n"
        "- Use live product/brand websites only as supporting visual evidence, not the whole report\n\n"
        "SOURCE_FETCH OPERATING RULE:\n"
        "- source_fetch supports query search plus direct url/urls batch fetch; use query to discover, then fetch the exact repo/file/doc URLs you want downstream nodes to implement from\n"
        "- When multiple builders exist, explicitly allocate different fetched sources to different builder handoffs instead of dumping one undifferentiated source list\n\n"
        "CRITICAL ROLE: You are not only researching. You are the EXECUTION ORCHESTRATOR — "
        "writing optimized downstream execution briefs, skill activation plans, and acceptance criteria "
        "for the other agents. Your report will be injected directly into builder/reviewer/tester/debugger prompts.\n\n"
        "Prompt-engineering standard you MUST follow:\n"
        "- state the concrete objective before style notes\n"
        "- separate hard constraints from optional inspiration\n"
        "- convert vague taste words into executable instructions\n"
        "- define what success looks like so reviewer/tester can enforce it\n"
        "- specify which runtime libraries each builder should load and whether they must be bundled locally (for games) or may stay remote (for websites/docs) and how to use them\n"
        "- specify exactly which pages/routes to create and what navigation structure connects them\n\n"
        "OPERATING MODEL:\n"
        "- Treat this as a lightweight SOP package for the downstream nodes, not a loose inspiration memo\n"
        "- Explicitly define deliverables, completion criteria, integration order, and likely risks\n"
        "- Treat any upstream planner skeleton or node brief as a hard contract unless it directly conflicts with the user's latest request\n"
        "- Assign specific responsibilities to each builder (if multiple) with clear scope boundaries\n\n"
        "SKILL ACTIVATION MANDATE:\n"
        "- You MUST include a <skill_activation_plan> section specifying which skills each node should activate\n"
        "- Available skills are loaded per-node based on task type; reference them by name\n"
        "- For websites: commercial-ui-polish, motion-choreography-system, visual-slot-recovery, conversion-surface-architecture\n"
        "- For reviewers: evidence-driven-qa, scroll-evidence-capture, design-system-consistency\n"
        "- Tell each node HOW to apply the skill, not just which one to load\n\n"
        f"{image_curation_hint}"
        "HARD CONSTRAINTS YOU MUST ENFORCE:\n"
        "- Generated pages must NEVER use emoji glyphs as icons, bullets, or decorative illustrations\n"
        "- Use inline SVG / CSS shapes / type treatment instead of emoji or cheap stock icon shortcuts\n"
        "- Favor premium, commercially credible layouts over generic student-project structure\n"
        "- Recommendations must be specific enough that downstream builder(s) can execute without re-research; if the plan later uses two builders, their scopes must still be separable cleanly\n"
        "- NAVIGATION COMPLETENESS: If requesting N pages, specify all N page filenames and ensure index.html navigation links to ALL of them. Missing nav links is a blocking failure.\n\n"
        f"{language_rule}"
        f"{game_research_rule}"
        f"{game_control_contract_rule}"
        "Output MUST use the exact XML tags below so downstream nodes can parse them:\n"
        "<reference_sites>\n"
        "- each visited URL + what it is useful for\n"
        "</reference_sites>\n"
        "<design_direction>\n"
        "- color system\n"
        "- typography direction\n"
        "- layout rhythm\n"
        "- motion principles\n"
        "- runtime libraries to load (local path or CDN) and their purpose\n"
        "</design_direction>\n"
        "<non_negotiables>\n"
        "- concrete quality bar and hard constraints\n"
        "</non_negotiables>\n"
        "<deliverables_contract>\n"
        "- exactly what artifacts / sections / interactions must exist before this task counts as done\n"
        "- complete list of HTML filenames (e.g., index.html, attractions.html, cities.html) and their nav structure\n"
        "</deliverables_contract>\n"
        "<curated_image_library>\n"
        "- for each page: hero image URL or composition plan, supporting image URLs or composition plan, background treatment (CSS gradients, SVG patterns)\n"
        "- include a one-line source note or confidence note for location-specific imagery\n"
        "- only use analyst-verified URLs or CSS/SVG compositions — NO random stock photos\n"
        "</curated_image_library>\n"
        "<skill_activation_plan>\n"
        "- for each node (builder_1, builder_2, reviewer, tester, polisher): which skills to activate and HOW to apply them\n"
        "- example: builder_1: motion-choreography-system → use GSAP ScrollTrigger for section reveals\n"
        "</skill_activation_plan>\n"
        "<risk_register>\n"
        "- likely failure points, hidden risks, and what the downstream nodes should watch carefully\n"
        "</risk_register>\n"
        + (
            # ── P0 FIX 2026-04-04: Task-type specific reference code & architecture tags ──
            # Inspired by MetaGPT's Architect→Engineer pattern: analyst provides executable
            # code blueprints so builders don't start from scratch.
            "<reference_code_snippets>\n"
            "MANDATORY for quality: provide working, copy-paste-ready code patterns (30-80 lines each) for:\n"
            "- Game loop: requestAnimationFrame structure with init/update/render cycle\n"
            "- State machine: all game states (MENU, PLAYING, PAUSED, GAME_OVER) with transitions\n"
            "- Input system: keyboard/mouse/touch binding pattern for this specific game type\n"
            "- Core mechanic: collision detection, scoring, or physics pattern matching the game genre\n"
            "- HUD rendering: health bar, ammo counter, score display pattern\n"
            "Code must compile standalone — no pseudocode, no 'implement here' placeholders.\n"
            "</reference_code_snippets>\n"
            "<game_mechanics_spec>\n"
            "- State machine: list ALL states and their transition triggers\n"
            "- Entity system: player, enemies, projectiles, collectibles — properties and update behaviors\n"
            "- Scoring & progression: how score increments, level progression, difficulty curve formula\n"
            "- Win/lose conditions: exact trigger conditions and restart/continue flow\n"
            "- Control mapping: WASD/arrows, mouse aim, space=jump, click=shoot, etc.\n"
            "- Combat fairness: startup safe window, enemy spawn distance rule, damage gating / invulnerability timing, and how the player avoids unavoidable spawn-kill openings\n"
            "</game_mechanics_spec>\n"
            "<control_frame_contract>\n"
            "- Define world axes / camera frame / screen-space expectations so movement and drag camera controls are not mirrored\n"
            "- State exact anti-mirror acceptance checks for W/A/S/D, drag right/left, drag up/down, and click/tap actions\n"
            "- Specify whether movement is camera-relative or world-relative and what 'forward' means at match start\n"
            "- Include startup safety expectations: first enemy contact timing, minimum spawn radius vs attack radius, and whether spawn grace / invulnerability is required\n"
            "</control_frame_contract>\n"
            "<asset_sourcing_plan>\n"
            "- List permissive asset/model/reference libraries, source_fetch-first URLs or repo/file anchors, and why they are allowed\n"
            "- Define fallback when images/models cannot be generated: authored procedural geometry, CSS/SVG compositions, or builder-side replacement hooks\n"
            "- State license expectations and runtime asset keys / filenames that must remain stable downstream\n"
            "</asset_sourcing_plan>\n"
            if profile.task_type == "game" else
            "<reference_code_snippets>\n"
            "Provide working code patterns for:\n"
            "- Reveal.js initialization with recommended config (theme, transition, plugins)\n"
            "- Sample slide with layout pattern (title + 2-column content + speaker notes)\n"
            "- Navigation and progress bar configuration\n"
            "</reference_code_snippets>\n"
            "<slide_deck_spec>\n"
            "- Total slide count and narrative arc (intro → body → conclusion)\n"
            "- Transition type per section (fade, slide, convex, zoom)\n"
            "- Reveal.js plugins to load: RevealMarkdown, RevealHighlight, RevealNotes, etc.\n"
            "- Speaker notes strategy\n"
            "</slide_deck_spec>\n"
            if profile.task_type == "presentation" else
            "<reference_code_snippets>\n"
            "Provide working code patterns (20-50 lines each) for:\n"
            "- The most complex interactive component (form validation, carousel, modal, filter)\n"
            "- Responsive layout: CSS Grid/Flexbox pattern for the main page structure\n"
            "- Navigation: responsive nav with mobile hamburger menu pattern\n"
            "</reference_code_snippets>\n"
            "<component_tree>\n"
            "- Page hierarchy and shared components (nav, footer, sidebar)\n"
            "- Responsive breakpoints: mobile (<768px), tablet (768-1024px), desktop (>1024px)\n"
            "- CSS custom properties / design tokens for consistent theming\n"
            "</component_tree>\n"
            if profile.task_type == "website" else
            "<reference_code_snippets>\n"
            "Provide working code patterns for:\n"
            "- Primary chart initialization (Chart.js or D3) with responsive config\n"
            "- Filter/state management pattern for dashboard interactivity\n"
            "- KPI card component with trend indicator\n"
            "</reference_code_snippets>\n"
            if profile.task_type == "dashboard" else
            ""
        ) +
        "<builder_1_handoff>\n"
        "- scope, priorities, must-build sections, visual rules, implementation hints\n"
        "- include 2-4 exact source URLs or repo/file anchors assigned to this builder and what each one is for\n"
        "- specific image URLs to use for each section\n"
        "- runtime libraries to load with exact local paths or script tags\n"
        "</builder_1_handoff>\n"
        "<builder_2_handoff>\n"
        "- scope, priorities, must-build sections, visual rules, implementation hints\n"
        "- include 2-4 exact source URLs or repo/file anchors assigned to this builder and what each one is for\n"
        "- if the final plan only uses one builder, mark this as N/A and put the full end-to-end build contract into builder_1_handoff\n"
        "</builder_2_handoff>\n"
        "- If the runtime plan contains 3+ builders, also emit additional tags like <builder_3_handoff>, <builder_4_handoff>, etc. to match the downstream builder slots exactly.\n"
        "<reviewer_handoff>\n"
        "- what quality issues to be strict about\n"
        "- image relevance check: verify all images match the site topic\n"
        "- navigation completeness check: verify all requested pages are linked from nav\n"
        "- skill/runtime compliance check: verify prescribed local runtimes or libraries are loaded and used\n"
        "</reviewer_handoff>\n"
        "<tester_handoff>\n"
        "- what interactions and edge cases must be verified\n"
        "</tester_handoff>\n"
        "<debugger_handoff>\n"
        "- likely failure points and how to repair them quickly\n"
        "</debugger_handoff>\n\n"
        f"Goal: {goal}"
    )


def tester_description() -> str:
    """Generate tester task description based on task type — called with goal at plan time."""
    # Returns a callable-like pattern; actual goal is injected at plan shape time
    return ""  # placeholder, actual usage is via profile.tester_hint


def design_requirements(goal: str) -> str:
    """Generate CONCISE design requirements (used in retries — keep short to avoid prompt bloat)."""
    profile = classify(goal)
    motion = motion_contract(goal).strip()
    if motion:
        return f"Quality bar ({profile.task_type}): {profile.quality}\n{motion}"
    return f"Quality bar ({profile.task_type}): {profile.quality}"
