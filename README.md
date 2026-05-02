# Evermind

> 一个把"一句需求"自动变成"完整可交付网站/网页游戏"的多智能体协作流水线。
> 本地优先 · 桌面端 · 8-12 节点 DAG · Pro 模式自动跑 30-50 分钟，输出生产级 HTML/CSS/JS 工程。

![Status](https://img.shields.io/badge/status-alpha-orange)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 关于作者（重要）

我是一名 **17 岁的高中生**，**不会写代码**，但对 AI 特别着迷。

Evermind 是我利用各种 AI 工具反复试错、迭代出来的项目。它的核心思路、架构决策、所有 bug 修复轮次（从 v6.x 一路到 v7.62），都是我反复推敲、形成方案、然后落地实现的——每一行代码我都尽力理解过它在做什么。

**老实说**：项目里还有很多功能我目前的知识水平优化不了。比如：
- 多模型路由 / token 缓存调度的更深入优化
- 浏览器自动化交互层的稳定性
- Three.js / WebGL 内容生成的进一步质量提升
- Python asyncio 的复杂死锁排查

我会**持续学习**，慢慢把这些短板补上。如果你是有经验的开发者，发现哪里写得不够好或有更好的做法，我特别欢迎你提 Issue 或直接 PR——你的每一条建议对我都是免费的"老师"。

**联系我**：可以通过 GitHub Issue 提建议，或在 Discussions 区聊聊。

---

## 这个项目能做什么

你给一句话或一段提示词，比如：

> "做一个未来科技 3D 网站，要 WebGL 三维背景、滚动驱动动画、暗色霓虹配色、首页+功能+定价三页、Awwwards 获奖级动效"

Evermind 的 12 个智能体角色 (按任务类型激活 8-12 个)会**协作**完成：

1. **Planner**（规划师）拆解需求成精确的执行蓝图——哪些页、每页要什么、哪些是必须、哪些是可选
2. **Analyst**（分析师）上网研究参考实现：Three.js 写法、相似 Awwwards 网站的技术栈
3. **UI Design**（UI 设计师）给出 Design Tokens（配色、字号、间距）+ 布局蓝图
4. **Scribe**（文案）写文案 + 内容架构
5. **Builder 1+2 并行**（构建者）写 HTML/CSS/JS 代码（按蓝图，主页 + 子页）
6. **Merger**（合并器）把两个 builder 的产出合并成统一项目
7. **Polisher**（抛光师）抛光：动效、留白、过渡（不改结构）
8. **Reviewer**（审查员）用 Playwright 浏览器实地审查质量并打分（0-10）
9. **Patcher**（补丁师）当 reviewer 不通过时，根据具体反馈做精确的 SEARCH/REPLACE 修复
10. **Deployer**（部署器）给出本地预览 URL
11. **Debugger**（调试器）修运行时报错
12. **Tester**（测试员）跑交互测试（点击/拖拽/键盘）

最终输出的是一个**真能在浏览器打开**的完整网站工程，含 index.html、styles.css、app.js、可能还有 about.html / pricing.html 等多页。

---

## 节点编排详解

### Pipeline 结构（pro 模式）

```
                                    ┌─→ imagegen ─┐
planner → analyst ──────────────────┼─→ spritesheet ──┐    (asset_heavy 任务)
                                    └─→ assetimport ──┘
                  │                                    │
                  └─→ uidesign + scribe ──┐            │
                                          ↓            ↓
                              ┌── builder 1 ──→ merger
                              └── builder 2 ──→
                                                │
                                                ↓
                              polisher (可选)
                                                │
                                                ↓
                                          reviewer ←──┐
                                                │     │
                                          ┌─────┤     │ (reject)
                                          │     ↓     │
                                       (pass)  patcher ┘ (最多 3 轮闭环)
                                          │
                                          ↓
                                       deployer → debugger → tester
```

### Reviewer ↔ Patcher 闭环（核心质量保证）

这是 Evermind 区别于"单 Agent 一次写完"的关键设计：

1. **Reviewer** 用真实浏览器打开 builder 产出，跑交互测试，输出结构化 JSON verdict（含 `blocking_issues` 数组，每条带 `file` / `anchor_line_range` / `suggested_fix`）
2. 如果 reject，**Patcher** 拿到这些精确指令，做 SEARCH/REPLACE 块级修复
3. 修完后 **Reviewer 重新审一遍**（不是简单接受 patcher 输出）
4. 这个闭环最多 3 轮，强制保证产出质量

**为什么不让一个 Agent 直接写到完美？**
LLM 一次性写复杂网站，平均质量是"能跑就停"。多 Agent 协作 + 严格门控让每个角色都聚焦自己擅长的事，最后再用浏览器实地验证。这是单 Agent 做不到的。

---

## 怎么用

### 1. 安装（macOS 桌面端）

下载 [Releases](https://github.com/Zhiyuan-511/Evermind/releases) 最新 DMG，拖进 Applications 即可。

首次启动会要求授权访问 Desktop 文件夹（用来保存生成的网站）。

### 2. 配置 API Key

打开 Evermind → Settings → 填入你的 LLM API key：
- **推荐**：Kimi (Moonshot) — `sk-...` — 国内速度最快、对中文 prompt 友好
- 也支持：OpenAI / Anthropic / DeepSeek / 通义千问等
- 如果用第三方中转站，填对应的 base URL 即可

### 3. 写提示词，开跑

**Simple 模式**（4 节点，3-5 分钟）：
适合简单页面，比如"一个计数器网页"、"咖啡店企业站三页"。

**Standard 模式**（6-8 节点，8-15 分钟）：
适合中等复杂度，含 reviewer 但没有 patcher 闭环。

**Pro 模式**（11 节点，30-50 分钟）：
完整流水线，含 reviewer↔patcher 闭环 + asset 管线。
适合：3D WebGL 网站、多页商业站、网页游戏、复杂 dashboard。

**Ultra 模式**：实验性，长任务（24h 上限）。

### 4. 提示词写得越详细，产出越好

Evermind 的 planner + builder 都会**严格按你的提示词执行**。建议参考：

```
角色：你是一位拥有 10 年经验的顶尖 WebGL 创意开发者
任务：未来科技 3D 网站
要求：
1. 用 Three.js 实现 hero 区 3D WebGL 背景
2. 滚动驱动的章节进入动画（IntersectionObserver / GSAP ScrollTrigger）
3. 至少三页：首页 / 功能 / 联系
4. 配色：暗色霓虹（charcoal + cyan #00f0ff + magenta #ff00aa）
5. Awwwards 获奖级丝滑过渡 + 微交互
```

提示词越具体，节点越听话——这是项目核心机制（v7.57 PLANNER BLUEPRINT 注入）的设计意图。

### 5. 自定义画布

也可以从空白画布开始，用 n8n 风格拖拽节点搭建你自己的 DAG。详见 [docs/NODE_GUIDE.md](./docs/NODE_GUIDE.md)。

---

## 架构亮点

### 12 个智能体 + 1 个动态调度器

每个智能体的 prompt 模板放在 `backend/prompt_templates/*.yaml`，可独立编辑+热重载。

调度器 (`backend/orchestrator.py`，~25K 行) 处理：
- DAG 拓扑排序 + 拓扑 reset（reviewer 触发后下游全部回到 PENDING）
- 节点超时 + 重试 + 模型 fallback 链
- 上下文传递（v7.57 主动注入 planner blueprint 给所有下游 code-producing 节点）
- 浏览器自动化 (Playwright) 给 reviewer 用

### Capability-Aware Builder（v7.56）

识别 10 类技术能力词（WebGL/Three.js / 2D 游戏 / Canvas 艺术 / GLSL Shader / Web Audio / 物理引擎 / Drag-Drop / 实时图表 / 视频嵌入 / 滚动驱动），自动给 builder system prompt 注入对应的"必须实现"契约。LLM 看到任务里有"3D 沉浸式"就会 inline 加载 Three.js 库 + 写真实 scene/camera/animate() 代码。

### Auto Post-Process（v7.56d / v7.58）

- 自动检测 HTML 含 `<canvas>` 但缺 Three.js library 标签 → 自动注入 `<script src=>`
- 自动把 `#canvas { z-index: 0 }` 改成 `z-index: -1`（防被 hero 内容遮挡）
- Three.js render call 自动加 null-guard

### Reviewer Fast-Path（v7.62 关键修复）

Reviewer 用 fast 非思考模型（kimi-k2.5）保证一定输出 JSON verdict。Thinking model 会写大量推理但忘记 JSON，导致 patcher 没 actionable brief 死循环。这是开源前最后一个核心修复。

---

## 已知限制（开源前如实告知）

- **平台**：目前只测过 macOS（arm64 + x64）。Windows/Linux 没测过，理论上 Python 后端可跑，但 Electron 前端要重打包。
- **3D 网站质量**：v7.62 后 builder 真会写 Three.js 代码了，但效果到 "Awwwards 中等" 水平。没到顶级 award-winning（这需要融合 lygia GLSL 库 + GSAP ScrollTrigger，开源后下一版做）。
- **Polisher 偶尔 timeout**：deterministic gap gate 检测不通过会 fail，但 pipeline 会跳过继续，不影响最终交付。
- **某些 LLM 的 JSON 输出稳定性**：尽管做了多重兜底（v7.56b ESCAPE HATCH + v7.59 顶部强约束 + forced-synthesis fallback），偶尔还是会遇到 LLM 不输出 JSON 的情况。
- **Token 消耗**：Pro 模式跑一轮大约消耗 200K-500K tokens（含 reviewer browser QA 多轮）。建议先用 Simple 模式打磨提示词，再上 Pro。

---

## 我会持续优化的方向

1. **多模型 ensemble** — 让 builder1 和 builder2 用不同模型（GPT/Kimi/Claude）然后让 merger 选最好的
2. **3D 资源库融合** — 集成 lygia GLSL + GSAP ScrollTrigger + Lenis 平滑滚动作为 first-class 砖块
3. **本地化 LLM 路由** — 国内用户直连 Kimi/Doubao/Qwen 不走中转
4. **更细的 reviewer rubric** — 把当前 6 维度评分扩展为 12 维度
5. **更稳的 Three.js / Canvas 后处理** — Bloom / FilmGrain / 后处理 EffectComposer 默认开

---

## 反馈渠道

- **GitHub Issues**：报 bug、提建议、聊架构
- **GitHub Discussions**：聊使用心得、贡献提示词模板
- **Pull Requests**：超级欢迎，尤其是 prompt 工程优化、新 capability 识别正则、postprocess 修复

我是新手，PR 我会**慢慢学着 review**——可能比经验丰富的项目慢一点，但我会认真看每一行你提的改动并向你请教。

---

## 致谢

- 各家 LLM 提供商让这一切可能（用户在 Settings 里自由切换）
- Three.js / GSAP / Playwright 等开源项目
- 所有提交 Issue 和 PR 的朋友——你们让一个高中生不孤单

---

## License

MIT — 自由使用、改造、分发。

---

> **如果你觉得这个项目有意思，star 一下让我知道**
> **如果你愿意指点一下哪里写得不对，我感激不尽**
