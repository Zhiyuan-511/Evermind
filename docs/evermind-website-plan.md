# Evermind 宣传网站 — 完整规划方案

> **目标**: 打造一个电影级水准的 8-10 页宣传网站  
> **工具**: Google Stitch (stitch.withgoogle.com)  
> **风格**: Apple 官网级极简 + 电影感动画 + 深色奢华科技感  
> **核心理念**: 用视觉讲故事，而非堆砌功能列表

---

## 一、Google Stitch 使用策略

### 关键注意事项
1. **逐页生成，不要一次出全站** — Stitch 最适合单页高保真生成，多页一次性出容易风格不统一
2. **先生成首页 Hero，定住设计语言** — 颜色、字体、间距确认后，后续页面保持一致
3. **上传参考截图** — 建议截取苹果官网、Linear.app、Vercel.com 的页面作为风格参考
4. **迭代优化** — 每页生成后微调，而不是期望一次到位
5. **导出 HTML/CSS** — Stitch 支持导出 HTML/Tailwind CSS 代码，导出后可手动加 GSAP 动画

### Stitch 输出格式建议
- 选择 **Experimental Mode**（更强的模型，更高保真）
- 输出尺寸选择 **Desktop (1440px)** 优先
- 每页保持同一色板和字体系统（在 prompt 中重复指定）

---

## 二、设计系统（所有页面共享）

### 色彩体系
```
主背景:       #0A0A0F（近黑，微蓝底）
次级背景:     #12121A（深灰蓝）
卡片/玻璃:    rgba(255,255,255,0.04) + backdrop-blur(24px)
主色调:       #6C5CE7 → #A855F7（紫色系渐变，代表 AI / 智能）
强调色:       #00D2FF（电光蓝，用于 CTA 和关键高亮）
成功/信号:    #10B981（翠绿）
文字主色:     #F0F0F5（近白）
文字次级:     #8B8B9E（浅灰紫）
分割线:       rgba(255,255,255,0.06)
```

### 字体系统
```
标题: SF Pro Display / Inter（Apple 风格无衬线）
正文: Inter / DM Sans
代码: JetBrains Mono
中文: 思源黑体 / Noto Sans SC
```

### 动效语言
```
页面转场:     View Transitions API + crossfade (400ms ease-out)
滚动动画:     IntersectionObserver + CSS transform 渐入
悬浮效果:     translateY(-4px) + box-shadow 扩展 (200ms cubic-bezier)
数据流动:     SVG animated dasharray + glow 脉冲
文字显现:     逐行 translateY(20px) + opacity fade-in (stagger 80ms)
按钮交互:     scale(1.02) + 光晕扩散 (150ms)
```

---

## 三、页面架构 & 逐页 Prompt

### 总览（10 页）

| # | 页面 | 定位 | 核心传达 |
|---|------|------|----------|
| 1 | **首页 / Hero** | 第一印象 | 「AI 自主创造的操作系统」|
| 2 | **产品展示** | 核心编辑器 | 可视化画布 + 节点编排 |
| 3 | **自主编排** | 差异化 | Plan→Build→Test→Fix 自愈循环 |
| 4 | **多模型网关** | 技术优势 | 100+ AI 模型，一键切换 |
| 5 | **安全与隐私** | 信任构建 | L1-L4 安全层 + 本地加密 |
| 6 | **用例场景** | 共情说服 | 3-4 个真实使用场景 |
| 7 | **技术架构** | 开发者信任 | Electron + Python + React |
| 8 | **定价方案** | 转化推动 | 免费 / Pro / Enterprise |
| 9 | **关于我们** | 品牌故事 | 团队 + 愿景 + 时间线 |
| 10 | **下载 / CTA** | 转化落地 | 一键下载 + Newsletter |

---

### Page 1: 首页 Hero（最关键的一页）

**视觉概念**: 深黑背景下，一个发光的画布编辑器缓缓从远处飘近，周围有数据流粒子在轨道上运动。中心的节点图谱发出柔和的紫色光晕。

**Stitch Prompt**:
```
Design a cinematic dark-mode hero landing page for "Evermind" — an AI workflow 
orchestrator desktop app.

Background: Deep black (#0A0A0F) with very subtle radial gradient glow in dark 
purple (#6C5CE7 at 5% opacity) centered behind the hero visual.

Navigation bar: Transparent/glassmorphism with the Evermind logo (a stylized "E" 
with neural glow) on the left, and 5 links (Product, Technology, Security, 
Pricing, Download) on the right. A glowing "Download Free" CTA button on the 
far right with purple-to-blue gradient border.

Hero section: 
- Headline: "Your Ideas. AI Builds. Autonomously." in very large (72px), 
  ultra-light weight, white text. Below in 24px gray (#8B8B9E): "The visual 
  operating system where AI agents plan, code, test, and ship — without human 
  intervention."
- Two CTA buttons: "Download for Mac" (filled, purple gradient) and 
  "Watch Demo" (glass/outline style, with a play icon).
- Below: a trust bar showing "100+ AI Models · Self-Correcting · Desktop-Native 
  · Enterprise Security"

Hero visual: A large, perspective-tilted screenshot of a dark-themed node editor 
canvas with connected AI agent nodes (glowing purple edges, glassmorphism node 
cards). The screenshot has a subtle shadow and floats with parallax depth.

Below the fold: Three feature pills in a horizontal row:
1. "Visual Workflow Editor" with a canvas icon
2. "Autonomous Orchestration" with a cycle/loop icon  
3. "100+ AI Models" with a grid/constellation icon

Overall aesthetic: Apple.com minimalism meets Linear.app dark elegance. 
Vast white space. No clutter. Premium.
Font: Inter or SF Pro Display. 
Color palette: deep blacks, purple accents (#6C5CE7), electric blue highlights 
(#00D2FF).
```

---

### Page 2: 产品展示 — 可视化编辑器

**视觉概念**: Apple 设备展示风格 — 一个巨大的 MacBook Pro 屏幕展示 Evermind 的画布编辑器，随着滚动，屏幕中的节点渐渐连接、激活、流动。

**Stitch Prompt**:
```
Design a product showcase page for "Evermind" visual editor (dark mode, same 
design system as hero — #0A0A0F background, purple accents).

Section 1 — "See It In Action":
Large centered headline "Design workflows visually. Execute autonomously." in 
white 56px. Subtitle: "Drag. Connect. Launch. Evermind's infinite canvas turns 
complex AI orchestration into visual poetry."

Below: A massive (full-width with padding) product screenshot showing a dark 
node editor with connected nodes (Router → Analyst → Builder × 2 → Reviewer → 
Deployer → Tester). Each node is a glassmorphism card with status indicators. 
The screenshot has rounded corners, a soft purple glow shadow, and appears 
floating above the dark background.

Section 2 — "22+ Specialized Nodes":
A grid of 6 feature cards (2x3), each with a glassmorphism background:
- "AI Agents" — Router, Builder, Tester, Reviewer (brain icon)
- "Code & Build" — Write, debug, deploy code (terminal icon)
- "Art Pipeline" — Generate images, sprites, assets (palette icon)
- "Local Execution" — Shell, file ops, git (laptop icon)
- "Browser Automation" — Navigate, scrape, interact (globe icon)
- "Security Layers" — L1-L4 permission gates (shield icon)

Each card: dark glass background, icon with purple glow, title in white, 
description in gray. Hover state: lift + expanded shadow.

Section 3 — Interactive Canvas Features:
Three features in alternating left-right layout:
1. "Infinite Canvas" — Pan, zoom, navigate with minimap
2. "Smart Connections" — Type-validated ports with animated data flow
3. "Real-time Preview" — Live HTML/CSS/game preview as AI builds

Aesthetic: Apple product page with the "step-through feature reveal" pattern.
```

---

### Page 3: 自主编排引擎

**视觉概念**: 循环流程图 — Plan → Distribute → Execute → Test → Fix → Deploy，每个节点在滚动时逐个点亮。

**Stitch Prompt**:
```
Design an "Autonomous Orchestration" page for Evermind (dark mode, deep black bg).

The core visual is a large circular diagram showing Evermind's self-healing 
pipeline: Plan → Build → Test → Fix → Ship, arranged in a glowing orbital loop.
Each stage is a node with an icon in a purple-bordered circle, connected by 
animated dashed lines with directional arrows. The center shows "Autonomous" 
in subtle glowing text.

Section headline: "AI That Fixes Its Own Code" in 56px white. 
Subtitle: "Submit a goal. Walk away. Evermind plans the architecture, assigns 
specialized agents, and iterates until the result passes quality gates."

Below the orbital: 4 step cards in horizontal timeline layout:
1. "You Describe" — "Tell Evermind what you want in plain language"
2. "AI Plans" — "The planner breaks your goal into subtasks with dependencies"
3. "Agents Execute" — "Builders code, reviewers inspect, testers verify"
4. "Self-Correct" — "Failed tests trigger automatic retry with diagnostic context"

Below: A comparison section "Before Evermind vs With Evermind":
- Before: "Copy code → paste to another AI → debug → repeat manually" (red tint)
- After: "Describe goal → autonomous pipeline → production-ready output" (green tint)

Bottom stat bar: "Up to 8 autonomous retries · Quality gates at every stage · 
Zero manual intervention required"

Style: Dramatic, cinematic, minimalist. Think Apple's chip announcement page.
```

---

### Page 4: 多模型网关 (100+ AI Models)

**Stitch Prompt**:
```
Design a "Multi-Model Gateway" page for Evermind (dark mode).

Hero: "One Interface. Every AI Model." in 56px white.
Subtitle: "GPT-5.4, Claude Opus, Gemini Pro, DeepSeek, Kimi, and 100+ more. 
Switch models per-node without changing a single line of configuration."

Visual: A constellation/galaxy visualization where each "star" is an AI model 
logo (OpenAI, Anthropic, Google, DeepSeek, Moonshot, etc.) floating in orbital 
paths around a central Evermind hub. The hub glows purple. Lines connect the hub 
to each model with data-flow animations.

Below: A table-style comparison showing model capabilities:
| Model | Speed | Code Quality | Creativity | Cost |
With visual bar indicators (not plain text).

Feature highlight: "Smart Model Routing"
"Evermind automatically selects the optimal model for each task type. 
Code tasks → DeepSeek/GPT. Creative → Claude. Research → Gemini."

Bottom: "Add your own API key. Pay only for what you use. 
No middleman markup." in clean typography.

Style: Dark, cosmic/constellation theme. Premium data visualization aesthetic.
```

---

### Page 5: 安全与隐私

**Stitch Prompt**:
```
Design a "Security & Privacy" page for Evermind (dark mode).

Headline: "Your Data Never Leaves Your Machine." in 56px white.
Subtitle: "Evermind runs entirely on your desktop. No cloud servers. No 
telemetry. Your API keys are encrypted at rest."

Visual: A large shield icon with 4 concentric layers, each representing a 
security level:
- L1 (outermost, green): "Read-Only — No confirmation needed"
- L2 (blue): "File & Network — Auto-approve configurable"
- L3 (orange): "System Commands — Explicit confirmation required"
- L4 (innermost, red): "Critical Actions — Physical switch + countdown"

Below: Three security feature cards:
1. "AES-128 Encryption" — "API keys encrypted with Fernet + PBKDF2"
2. "Privacy Masking" — "Sensitive data redacted before API dispatch"  
3. "Local-First" — "Python backend runs as a local sidecar process"

Trust bar: "SOC 2 Compliant Design · Zero Cloud Dependencies · 
Open-Source Auditable"

Style: Clean, authoritative, trust-building. Navy/dark background with 
green/blue security accents. NO flashy animations — communicate stability.
```

---

### Page 6: 用例场景

**Stitch Prompt**:
```
Design a "Use Cases" page for Evermind (dark mode).

Headline: "Built for Builders" in 56px white.

4 use case cards in a stacked layout (one per viewport height):

1. "Web Development" — Full-width card with glowing screenshot
   "Describe your website. Watch AI build 8+ pages with navigation, animations,
   and responsive design — in under 10 minutes."

2. "Game Development" — 
   "From concept to playable HTML5 game. Evermind generates game logic, HUD, 
   sprites, and game states autonomously."

3. "Data Dashboards & Tools" —
   "Build interactive dashboards, admin panels, and internal tools with 
   real-time data visualizations."

4. "Design Systems & Prototypes" —
   "Generate design tokens, component libraries, and high-fidelity prototypes 
   from natural language descriptions."

Each card: glassmorphism background, large visual on one side, text on the other.
Alternating left-right layout for visual rhythm.

Style: Apple "iPhone features" page — each section takes up the full viewport.
```

---

### Page 7: 技术架构

**Stitch Prompt**:
```
Design a "Technology" page for Evermind (dark mode, developer-focused).

Headline: "Built with Precision" in 56px white.

Architecture diagram: A clean layered visualization:
- Top: "Electron Shell" (desktop icon)
- Middle: "React Frontend" <-> "FastAPI Backend" (WebSocket)
- Bottom: Plugin System — file_ops, browser, shell, git, computer_use
- Connected to: "LiteLLM Gateway" → multiple model providers

Tech stack grid (6 items): Electron, React+Next.js, Python+FastAPI, LiteLLM, 
Playwright, WebSocket.

Code snippet section with syntax-highlighted workflow JSON.

Stats: "< 300ms node dispatch · 60fps canvas · 295MB app bundle"

Style: GitHub/Vercel-style developer documentation aesthetic.
```

---

### Page 8: 定价方案

**Stitch Prompt**:
```
Design a "Pricing" page for Evermind (dark mode).

Headline: "Start Free. Scale When Ready." in 56px white.

Three pricing cards:
1. "Free" ($0/forever) — glass card, standard border
2. "Pro" ($29/month) — PURPLE GRADIENT BORDER, "Most Popular" badge
3. "Enterprise" (Custom) — glass card, blue border

Below: FAQ accordion (5 questions about API keys, data privacy, models, etc.)

Style: Pro card visually stands out with glowing purple border and slight scale.
```

---

### Page 9: 关于我们

**Stitch Prompt**:
```
Design an "About" page for Evermind (dark mode).

Headline: "The Future of Creation" in 56px white.

Mission quote block with glassmorphism background.

Timeline: 2025 Q4 → 2026 Q4 journey, vertical glowing purple line.

Team section: 3-4 member cards.

Style: Warm, human, premium.
```

---

### Page 10: 下载 / Final CTA

**Stitch Prompt**:
```
Design a "Download" CTA page for Evermind (dark mode, cinematic).

Background: Subtle animated gradient — dark purple to deep blue.

Center: Evermind logo (large, glowing) + "Ready to Build the Future?" 64px white.

Download buttons: macOS (filled purple), Windows (outline), Linux (outline).
App size: "295MB · macOS 12+ / Windows 10+ / Ubuntu 20+"

Newsletter signup + Footer with social links.

Style: Grand finale. Emotionally compelling.
```

---

## 四、页面转场 & 动画策略

### Stitch 生成后需要手动添加的动效

| 动效 | 技术 | 优先级 |
|------|------|--------|
| 页面间转场 | View Transitions API | P0 |
| 滚动渐入 | IntersectionObserver + CSS | P0 |
| 节点流动动画 | SVG dasharray + GSAP | P1 |
| 产品截图视差 | CSS transform3d | P1 |
| 数字跳动 | CountUp.js | P2 |
| 背景粒子 | Three.js / CSS | P2 |
| 光标跟踪光效 | CSS radial-gradient | P3 |

### 推荐动画库
- **GSAP + ScrollTrigger** — 滚动驱动的复杂动画
- **View Transitions API** — 原生页面切换
- **Lottie** — 矢量 icon 动画
- **CSS @keyframes** — 简单循环动画

---

## 五、Stitch 使用步骤

1. **定义设计系统**: 先生成 Hero → 确认颜色/字体/间距
2. **逐页生成**: 每页带上 `Same design system: #0A0A0F bg, Inter font, purple accents...`
3. **导出增强**: 导出 HTML → 添加共享导航 → 加 scroll-reveal.js → 加 View Transitions
4. **资产替换**: 用 Evermind 真实截图替换占位图

---

## 六、时间表

| 天 | 任务 | 输出 |
|----|------|------|
| Day 1 上午 | Page 1-3 生成 + 迭代 | Hero + Product + Orchestration |
| Day 1 下午 | Page 4-6 生成 + 迭代 | Models + Security + Use Cases |
| Day 2 上午 | Page 7-10 生成 + 迭代 | Technology + Pricing + About + Download |
| Day 2 下午 | 导出 + 动画增强 + 资产替换 | 完整可交付网站 |

---

## 七、进阶方案

> [!TIP]
> **方案 B**: 让 Evermind 给自己做宣传网站 — 这本身就是最好的 demo。如果能用 Evermind 在 10 分钟内生成自己的宣传网站，这就是最有说服力的展示。

> [!IMPORTANT]
> **决策点**: 网站语言用纯英文还是中英双语？这会影响所有 Stitch prompt 的文案内容。
