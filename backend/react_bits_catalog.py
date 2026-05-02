"""v5.5 React Bits catalog — injected into the Builder system prompt so it
can compose pages from 100+ battle-tested animated components rather than
reinventing visuals from scratch.

Source: https://github.com/DavidHDev/react-bits (MIT, 38k+ stars)
Install: https://reactbits.dev/get-started/installation
CLI: `npx jsrepo add https://reactbits.dev/default/<Category>/<Name>`

We embed a compact index (name + category + 1-line description) rather than
the full source, because:
  1. It fits in a few KB of prompt context.
  2. Builder node can pick components from the index and have Kimi/GPT
     write the minimal wiring — React Bits components are small and
     self-contained.
  3. If the user prefers vanilla HTML/CSS output, Builder simply ignores
     this block. No hard runtime dependency introduced.

Usage pattern (emitted by Builder):
  - For a React/Next project: `npx jsrepo add https://reactbits.dev/default/TextAnimations/BlurText`
    then `import BlurText from './components/TextAnimations/BlurText/BlurText'`.
  - For a single-file HTML page: read the component source from the repo
    and inline the effect manually (fall back to custom CSS/JS).

v5.5: all component names verified against reactbits.dev as of 2026-04.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

# Grouped by category. Each entry: (name, one-line effect description).
REACT_BITS: Dict[str, List[Tuple[str, str]]] = {
    "TextAnimations": [
        ("BlurText", "Words fade in with a gaussian-blur reveal"),
        ("SplitText", "Characters/words split & stagger in individually"),
        ("SparklesText", "Text covered with animated sparkle particles"),
        ("ShinyText", "Horizontal metallic sheen sweeping over glyphs"),
        ("GradientText", "Animated gradient filling the text"),
        ("TypeText", "Typewriter effect with caret"),
        ("RotatingText", "Word rotator with smooth crossfade"),
        ("NumberTicker", "Count-up / count-down animated number"),
        ("DecryptedText", "Scramble→decrypt reveal (Matrix style)"),
        ("TextPressure", "3D-pressure variable-font hover effect"),
        ("ScrambleHover", "Character-scramble on hover"),
        ("CurvedLoop", "Text curved along a looping path"),
        ("VariableProximity", "Font weight tracks cursor proximity"),
        ("CountUp", "Count-up animation on viewport enter"),
        ("TrueFocus", "Spotlight focus on one word at a time"),
        ("ScrollReveal", "Progressive text reveal on scroll"),
        ("GlitchText", "RGB-split glitch effect"),
        ("FuzzyText", "Neon fuzzy shadow text"),
        ("TextCursor", "Cursor trail of glyphs"),
        ("ASCIIText", "ASCII-art font render"),
    ],
    "Animations": [
        ("ClickSpark", "Click-triggered spark burst"),
        ("Magnet", "Magnetic cursor attraction on hover"),
        ("MagnetLines", "Grid of lines attracted to cursor"),
        ("AnimatedContent", "Universal fade/slide-in on mount"),
        ("FadeContent", "Simple IntersectionObserver fade-in"),
        ("PixelTransition", "Pixelated mosaic page transition"),
        ("GlareHover", "Glossy glare sweep on hover"),
        ("PixelTrail", "Cursor leaves pixel trail"),
        ("StarBorder", "Animated star-border gradient"),
        ("MetaBalls", "Liquid metaball blobs"),
        ("BlobCursor", "Smooth blob following cursor"),
        ("SplashCursor", "Fluid splash on cursor move"),
        ("ImageTrail", "Images follow cursor in trail"),
        ("Ribbons", "Animated ribbon physics"),
        ("Cubes", "3D rotating cube grid"),
        ("TargetCursor", "Target-reticle cursor replacement"),
        ("CrosshairCursor", "Crosshair cursor replacement"),
        ("ShapeBlur", "Morphing blurred shape"),
        ("LogoLoop", "Infinite logo carousel"),
        ("LaserFlow", "Laser-beam flowing lines"),
    ],
    "Backgrounds": [
        ("Aurora", "Animated aurora-borealis gradient"),
        ("Beams", "Moving light beams"),
        ("Particles", "Interactive particle field"),
        ("Squares", "Tessellated animated square grid"),
        ("Hyperspeed", "Tunnel/hyperspeed star effect"),
        ("Galaxy", "Twinkling galaxy starfield"),
        ("LightRays", "Radial light rays"),
        ("DotGrid", "Animated dot grid"),
        ("Threads", "Animated thread strands"),
        ("Iridescence", "Iridescent moving surface"),
        ("LiquidChrome", "Liquid-chrome wave"),
        ("Waves", "Smooth wave lines"),
        ("Silk", "Silky gradient noise"),
        ("Orb", "Glowing orb focal point"),
        ("GradientMesh", "WebGL gradient mesh"),
        ("Lightning", "Electric lightning strikes"),
        ("Plasma", "Plasma turbulence"),
        ("DarkVeil", "Dark atmospheric veil"),
        ("LiquidEther", "Ethereal liquid surface"),
        ("Balatro", "Balatro-style card backdrop"),
        ("RippleGrid", "Ripple propagation over grid"),
        ("Ballpit", "Physics ball pit"),
        ("LetterGlitch", "Letter-matrix glitch background"),
    ],
    "Components": [
        ("SplashCursor", "Cursor-driven fluid splash"),
        ("Carousel", "Scroll-snap image carousel"),
        ("CardSwap", "Swappable stacked cards"),
        ("Dock", "macOS-style magnifying dock"),
        ("FluidGlass", "Glassmorphic fluid container"),
        ("FlyingPosters", "Posters flying across viewport"),
        ("InfiniteMenu", "Radial infinite rotating menu"),
        ("InfiniteScroll", "Infinite vertical/horizontal scroll"),
        ("ChromaGrid", "Chromatic hover grid"),
        ("Stepper", "Animated wizard stepper"),
        ("TiltedCard", "3D tilt-on-hover card"),
        ("ProfileCard", "Animated profile card"),
        ("Stack", "Stacked card pile"),
        ("Folder", "Folder-open reveal UI"),
        ("Lanyard", "Lanyard-style nametag"),
        ("PillNav", "Pill-style nav bar"),
        ("GooeyNav", "Gooey bubble navigation"),
        ("BounceCards", "Bouncing card row"),
        ("MagicBento", "Bento grid with magic reveal"),
        ("RollingGallery", "Rolling image gallery"),
        ("ElasticSlider", "Elastic range slider"),
        ("Counter", "Animated counter widget"),
        ("ModelViewer", "3D model viewer"),
        ("Masonry", "Masonry image grid"),
        ("PixelCard", "Pixelated hover card"),
    ],
}


def list_categories() -> List[str]:
    return list(REACT_BITS.keys())


def count_components() -> int:
    return sum(len(v) for v in REACT_BITS.values())


def build_prompt_block(lang: str = "zh", max_per_category: int = 10) -> str:
    """Render the catalog as a markdown block for Builder / UIDesign / Analyst
    system prompts. Keep it compact (roughly 1-2KB) so it doesn't dominate the
    context window.

    v6.4.11 (maintainer): rewrote header so the catalog doubles as a
    design vocabulary (uidesign / analyst) AND an implementation reference
    (builder / polisher / merger). Previously the "ignore if vanilla HTML"
    instruction stripped the catalog's value for any non-React pipeline,
    which is most of Evermind's runs.

    lang: 'zh' or 'en' — header text follows UI language. Component names
    stay in English (they are import identifiers / design-vocabulary tags)."""
    is_en = str(lang or "zh").lower().startswith("en")
    header_en = (
        "\n\n## React Bits — Visual Component Catalog (100+ entries)\n"
        "Source: reactbits.dev (MIT license). Use this catalog in TWO ways:\n"
        "  1. **As design vocabulary** (uidesign / analyst / reviewer briefs): "
        "reference components by name when describing interactions — e.g. "
        "`Magnet` for magnetic hover buttons, `Aurora` for cinematic hero "
        "backgrounds, `MagicBento` for feature grids, `SplitText` for letter-"
        "staggered headlines. This gives builders a concrete target.\n"
        "  2. **As implementation reference** (builder / polisher / merger):\n"
        "     - For **React/Next** stacks: install with "
        "`npx jsrepo add https://reactbits.dev/default/<Category>/<Name>` "
        "and import the component directly.\n"
        "     - For **vanilla HTML** stacks (Evermind's default): the effect "
        "pattern is still the reference — port the idea into inline CSS/JS "
        "(e.g. `Magnet` → `transform: translate(...)` driven by mouse offset, "
        "`Aurora` → animated radial gradients, `SplitText` → span-per-character "
        "+ staggered `animationDelay`). Do NOT fake React imports in an HTML "
        "build.\n"
    )
    header_zh = (
        "\n\n## React Bits 视觉组件库(100+ 条目)\n"
        "来源:reactbits.dev(MIT 协议)。这个目录有两种用法:\n"
        "  1. **作为设计语汇**(uidesign / analyst / reviewer 简报):"
        "在描述交互时直接引用组件名,例如 `Magnet` 表示磁性按钮,"
        "`Aurora` 表示电影级 hero 背景,`MagicBento` 表示 feature 栅格,"
        "`SplitText` 表示字符错落入场标题。这样 builder 有明确目标。\n"
        "  2. **作为实现参考**(builder / polisher / merger):\n"
        "     - **React/Next 技术栈**:用 "
        "`npx jsrepo add https://reactbits.dev/default/<Category>/<Name>` 安装,"
        "直接 import。\n"
        "     - **纯 HTML 技术栈**(Evermind 默认):组件效果仍然是参考,"
        "把效果思路内联到 CSS/JS(例如 `Magnet` → 鼠标偏移驱动 `transform: "
        "translate(...)`;`Aurora` → 动画径向渐变;`SplitText` → 每字符一个 "
        "`<span>` + 阶梯 `animationDelay`)。**不要**在 HTML 项目里伪造 React import。\n"
    )
    parts: List[str] = [header_en if is_en else header_zh]
    for category, entries in REACT_BITS.items():
        shown = entries[: max(1, int(max_per_category))]
        parts.append(f"\n### {category}\n")
        for name, desc in shown:
            parts.append(f"- `{name}` — {desc}")
        remaining = len(entries) - len(shown)
        if remaining > 0:
            more = f"_(+{remaining} more in this category)_" if is_en else f"_(此分类还有 {remaining} 个)_"
            parts.append(more)
    return "\n".join(parts)


def is_enabled(config: Dict | None = None) -> bool:
    """Allow users to opt-out via EVERMIND_DISABLE_REACT_BITS=1 or config field.
    Default: enabled (it's a small prompt cost with big upside for React builds)."""
    env = str(os.environ.get("EVERMIND_DISABLE_REACT_BITS", "")).strip().lower()
    if env in ("1", "true", "yes", "on"):
        return False
    if isinstance(config, dict):
        flag = config.get("disable_react_bits")
        if isinstance(flag, bool) and flag:
            return False
        if isinstance(flag, str) and flag.strip().lower() in ("1", "true", "yes"):
            return False
    return True


__all__ = ["REACT_BITS", "list_categories", "count_components", "build_prompt_block", "is_enabled"]
