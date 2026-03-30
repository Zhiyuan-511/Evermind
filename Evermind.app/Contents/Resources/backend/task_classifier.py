"""
Task Type Classifier + Specialized Prompt Templates

Detects the user's intent from goal text and provides task-specific
design guidance, structure blueprints, and quality criteria.
"""

import re
from pathlib import Path
from typing import Dict, List, NamedTuple

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
    r"角色素材|敌人素材|boss素材|特效素材|素材包|asset pack)",
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
    r"(3d asset|3d model|3d character|3d enemy|model pack|weapon model|character model|enemy model|"
    r"建模|模型|角色模型|怪物模型|武器模型|材质|贴图|纹理|rig|rigging|动画片段|animation clip|"
    r"low[- ]?poly|low poly|voxel asset|体素素材|prop pack|environment pack)",
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
            "      For landmark / destination / history pages, use ONLY user-provided or analyst-verified image URLs.\n",
            "      Do NOT invent remote photo URLs for a city or landmark just because they look plausible.\n",
            "      If exact place imagery is not confidently verified, ship a premium non-photo composition with location captioning instead of a wrong photo.\n",
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


def game_asset_pipeline_mode(goal: str) -> str:
    """Return the asset-pipeline mode: '', 'image', '2d', or '3d'."""
    text = str(goal or "")
    if not text:
        return ""
    profile = classify(text)
    if profile.task_type != "game":
        return "image" if _NON_GAME_GENERATED_ASSET_RE.search(text) else ""

    has_2d_asset_request = bool(_GAME_EXPLICIT_ASSET_PIPELINE_RE.search(text))
    has_3d_asset_request = bool(_GAME_3D_ASSET_PIPELINE_RE.search(text))
    is_3d_or_procedural = bool(_GAME_PROCEDURAL_OR_3D_RE.search(text))
    is_2d_override = bool(_GAME_2D_SPRITE_OVERRIDE_RE.search(text))

    if has_2d_asset_request and (not is_3d_or_procedural or is_2d_override):
        return "2d"
    if has_3d_asset_request:
        return "3d"
    if has_2d_asset_request:
        return "2d"

    return ""


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
        r"(游戏|game|play|pixel|像素|弹球|贪吃蛇|snake|tetris|俄罗斯方块|打飞机|射击|"
        r"platformer|跑酷|flappy|pong|breakout|chess|棋|card game|纸牌|rpg|冒险|"
        r"arcade|迷宫|maze|puzzle game|益智游戏|打砖块|消消乐|match-3|tower defense|塔防)",
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
        r"color picker|取色|unit convert|翻译|translator|encoder|decoder)",
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
            "(9) External libraries are loaded only when prescribed and actually used."
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
            "A. Use <canvas> for rendering (2D context) OR pure CSS/DOM for simple games\n"
            "B. Implement a proper game loop: requestAnimationFrame with delta time\n"
            "C. State machine: MENU → PLAYING → PAUSED → GAME_OVER\n"
            "D. Keyboard/touch input handling with event listeners on document (NOT canvas)\n"
            "E. Score system with visual HUD overlay\n"
            "F. Collision detection (AABB or distance-based)\n"
            "G. Particle effects for impacts/explosions/scoring\n"
            "H. Sound: use Web Audio API oscillator for retro SFX (no external files)\n"
            "   OR use howler.js CDN for richer audio: <script src='https://cdn.jsdelivr.net/npm/howler@2.2.4/dist/howler.min.js'></script>\n"
            "I. Color palette: use a cohesive game palette (e.g. pico-8 inspired)\n"
            "J. Pixel-perfect rendering: image-rendering: pixelated for retro; smooth for modern\n"
            "K. Start screen with title + clickable 'Start Game' button (MUST use onclick handler)\n"
            "   IMPORTANT: Start button MUST work via mouse click, not only keyboard!\n"
            "   The game may run inside an iframe where keyboard focus requires a click first.\n"
            "L. Game over screen with score + high score (localStorage) + restart button (clickable)\n"
            "M. Keyboard listeners MUST be on document.addEventListener('keydown', ...) not on canvas\n"
            "N. Auto-focus: when game starts, call canvas.focus() and add tabindex='0' to canvas\n"
            "O. If custom art is required but no external asset generator is attached, create high-quality SVG or pixel placeholders\n"
            "   with a clear asset manifest so imagegen / spritesheet nodes can replace them later without re-architecting the game\n"
            "P. RECOMMENDED ENGINE CDNs — choose based on game type:\n"
            "   2D games (platformer, shooter, puzzle):\n"
            "     <script src='https://cdn.jsdelivr.net/npm/phaser@3.80.1/dist/phaser.min.js'></script>\n"
            "     Phaser provides built-in physics (Arcade/Matter), sprite animation, tilemaps, and audio.\n"
            "   3D games (FPS, racing, RPG):\n"
            "     <script src='https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.min.js'></script>\n"
            "     <script src='https://cdn.jsdelivr.net/npm/cannon-es@0.20.0/dist/cannon-es.cjs.min.js'></script>\n"
            "     Three.js + cannon-es for 3D rendering with realistic physics.\n"
            "   Free game assets (CC0, no attribution needed):\n"
            "     Kenney.nl assets: https://kenney.nl/assets (2D sprites, 3D models, UI, audio)\n"
            "     Use direct PNG/SVG URLs: https://kenney.nl/media/pages/assets/...\n"
            "   For simple games (snake, tetris, minesweeper), vanilla Canvas 2D API is fine — no engine CDN needed.\n"
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
            "E. Copy-to-clipboard buttons on outputs (with ✓ feedback animation)\n"
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

    # Keep explicit product-shape tasks deterministic. A "website with animation"
    # is still a website and should keep the website delivery contract.
    for task_type in ("game", "dashboard", "tool", "presentation"):
        pattern = pattern_map.get(task_type)
        if pattern and pattern.search(text):
            return PROFILES[task_type]

    website_pattern = pattern_map.get("website")
    if website_pattern and website_pattern.search(text):
        return PROFILES["website"]

    creative_pattern = pattern_map.get("creative")
    if creative_pattern and creative_pattern.search(text):
        return PROFILES["creative"]

    # Default to website
    return PROFILES["website"]


def builder_system_prompt(goal: str) -> str:
    """Generate a task-adaptive builder system prompt WITH injected CSS template.
    The CSS template provides pre-built professional styling so the model
    only needs to write HTML structure and content."""
    profile = classify(goal)

    # Load pre-built CSS templates
    base_css = _load_template("base.css")
    # Map task types to template files
    type_css_map = {
        "website": "website.css",
        "presentation": "presentation.css",
        "game": "game.css",
        "dashboard": "dashboard.css",
        "tool": "website.css",  # tools use website base
        "creative": "game.css",  # creative uses game base
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
            "- For games, the first saved HTML must already render a playable shell: start screen, gameplay viewport/canvas or arena, HUD, and at least one visible gameplay entity or stage element.\n"
            "- Keep game UI CSS lightweight on the first pass; prioritize working gameplay shell and visible body content over large decorative style systems.\n"
        )

    # §P1-FIX: Replace the {{TOPIC_IMAGE_BLOCK}} placeholder with topic-aware imagery
    design_system_with_images = profile.design_system.replace(
        "{{TOPIC_IMAGE_BLOCK}}", _topic_image_block(goal)
    )

    return (
        f"{profile.role}\n"
        f"{_COMMON_RULES}"
        f"{css_block}"
        f"{design_system_with_images}"
        f"{profile.blueprint}"
        f"{first_write_contract}"
        f"{multi_page_block}"
        f"{motion_block}"
        f"{language_block}"
        f"Quality: {profile.quality}\n"
        f"{delivery_block}"
    )


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
        )

    return (
        f"{scope_line}"
        f"{delivery_line}"
        f"{language_line}"
        "Follow the design system and structure rules from your system prompt. "
        "Treat any upstream planner/analyst notes, loaded skills, reviewer blockers, and acceptance criteria as hard requirements, not optional inspiration. "
        "Do not use emoji glyphs in the generated product; use SVG/CSS alternatives instead. "
        "Make the result materially complete: real sections, real content, real interactions, and visible polish. "
        f"{compact_first_save_line}"
        f"{motion_line}"
        "If the brief requests multiple pages/routes, do NOT fake it as one long landing page; every required page must exist and be reachable from navigation. "
        "Keep styling preview-safe: include an inline <style> block or write a local stylesheet file; do not depend on Tailwind CDN or other remote CSS runtimes as the main styling path. "
        "IMAGE SIZING: All <img> must have max-width:100%;height:auto. Hero images use object-fit:cover inside a fixed-height container (60vh). Unsplash URLs: heroes ?w=1600&h=900&fit=crop, cards ?w=800&h=500&fit=crop. Never use ?w=1920 or bare uncontained images. "
        "IMAGE TRUTH RULE: For landmark/location-specific imagery, use only user-provided or analyst-verified URLs. If you cannot verify an exact image, build a premium CSS/SVG composition instead of using a wrong photo. "
        "For game or asset-heavy tasks, preserve a clean asset manifest / placeholder structure so dedicated asset nodes can upgrade art without rewriting the core logic. "
        "RESOURCE INTEGRITY: Do NOT reference local files (images, fonts, JS) that do not exist in the output directory; use CDN URLs, inline SVG, or CSS compositions instead of invented local paths like 'hero-bg.jpg' or 'logo.png'. "
        "SVG SIZING: All inline <svg> elements MUST have explicit width and height attributes (e.g. width='48' height='48'). A viewBox alone is NOT enough — SVG without width/height expands to fill its container and creates oversized shapes. For decorative icons use 24-48px, for feature icons use 48-64px, for hero artwork use responsive CSS (max-width:200px). NEVER leave an SVG without width and height. "
        "After saving, briefly describe exactly what you built and what quality checks you satisfied."
    )


def analyst_description(goal: str) -> str:
    """Generate analyst task description based on task type."""
    profile = classify(goal)
    game_research_rule = ""
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
            "- If you inspect a reference page, extract mechanics/UI/asset insights quickly and move on\n\n"
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
        "CRITICAL ROLE: You are not only researching. You are the EXECUTION ORCHESTRATOR — "
        "writing optimized downstream execution briefs, skill activation plans, and acceptance criteria "
        "for the other agents. Your report will be injected directly into builder/reviewer/tester/debugger prompts.\n\n"
        "Prompt-engineering standard you MUST follow:\n"
        "- state the concrete objective before style notes\n"
        "- separate hard constraints from optional inspiration\n"
        "- convert vague taste words into executable instructions\n"
        "- define what success looks like so reviewer/tester can enforce it\n"
        "- specify which CDN libraries each builder should load (GSAP, AOS, anime.js, etc.) and how to use them\n"
        "- specify exactly which pages/routes to create and what navigation structure connects them\n\n"
        "OPERATING MODEL:\n"
        "- Treat this as a lightweight SOP package for the downstream nodes, not a loose inspiration memo\n"
        "- Explicitly define deliverables, completion criteria, integration order, and likely risks\n"
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
        "Output MUST use the exact XML tags below so downstream nodes can parse them:\n"
        "<reference_sites>\n"
        "- each visited URL + what it is useful for\n"
        "</reference_sites>\n"
        "<design_direction>\n"
        "- color system\n"
        "- typography direction\n"
        "- layout rhythm\n"
        "- motion principles\n"
        "- CDN libraries to load and their purpose\n"
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
        "<builder_1_handoff>\n"
        "- scope, priorities, must-build sections, visual rules, implementation hints\n"
        "- specific image URLs to use for each section\n"
        "- CDN libraries to load with exact script tags\n"
        "</builder_1_handoff>\n"
        "<builder_2_handoff>\n"
        "- scope, priorities, must-build sections, visual rules, implementation hints\n"
        "- if the final plan only uses one builder, mark this as N/A and put the full end-to-end build contract into builder_1_handoff\n"
        "</builder_2_handoff>\n"
        "<reviewer_handoff>\n"
        "- what quality issues to be strict about\n"
        "- image relevance check: verify all images match the site topic\n"
        "- navigation completeness check: verify all requested pages are linked from nav\n"
        "- skill compliance check: verify prescribed CDN libraries are loaded and used\n"
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
