# Evermind 节点编排指南(中文)

这份指南给你讲清楚 Evermind 的 13 个节点各自能干什么、怎么串起来才跑得通、哪些坑要避开。读完后你就可以自信地在画布上搭自己的工作流。

## 快速开始 —— 3 步上路

1. **选模板**:顶栏"模板"入口打开 Template Gallery,挑一个和你目标最接近的模板。模板会把节点直接铺到画布上。
2. **调节点**:画布上的节点可以拖拽、增删、连边。节点之间的有向箭头代表依赖(上游完成才启动下游)。
3. **点运行**:在右下输入你的目标,点 Run。Evermind 会把你当前的画布作为 **custom plan** 发给后端,Planner 会严格按照你这张图分派任务。

## 节点一览

| 节点 | 做什么 | 典型上游 | 典型下游 |
|---|---|---|---|
| `router` | 入口节点,判定任务类型 + 分派模型 | —(入口) | `planner` |
| `planner` | 把目标拆成子任务,产出 JSON 蓝图 | `router` | `analyst` / `builder` / `imagegen` |
| `analyst` | 调研参考站点、沉淀设计方向 | `planner` | `builder` 或 `imagegen` |
| `uidesign` | 产出 UI 设计 brief(色板、字体、栅格) | `analyst` | `builder` |
| `imagegen` | 规划/生成 AI 图片与视觉素材 | `planner` 或 `analyst` | `spritesheet` / `assetimport` / `builder` |
| `spritesheet` | 拼接精灵表(2D 游戏素材) | `imagegen` | `assetimport` / `builder` |
| `assetimport` | 组织 3D / 游戏资产包 | `analyst` + `imagegen` + `spritesheet` | `builder` |
| `builder` | 写真实代码(HTML/CSS/JS) | `analyst` / `assetimport` | `merger` / `reviewer` |
| `merger` | 合并多个 builder 的产出为单一 index.html | 多个 `builder` | `reviewer` |
| `reviewer` | 严格质量门(内置浏览器跑 QA) | `merger` 或 `builder` | `tester` / `debugger` / `deployer` |
| `tester` | 跑功能测试 + 交互验证 | `reviewer` | `debugger` 或 `deployer` |
| `debugger` | 针对 reviewer/tester 报的问题做修复 | `reviewer` / `tester` | `deployer` |
| `deployer` | 打包 + 生成预览链接(交付产物) | 所有 QA 都过后 | —(终点) |
| `polisher` | 局部抛光(字号/间距/动效) | `builder` 或 `merger` | `reviewer` |
| `scribe` | 产出 README / 使用文档 | `builder` | `deployer` |

## 合法的 DAG 形状

- **必须有入口节点**:通常是 `router`(自动判任务类型)。如果你绝对知道任务性质,也可以跳过 `router`,直接 `planner`。
- **必须有终点节点**:`deployer` 是默认终点;如果只是写个单文件也可以让 `reviewer` 作终点(不部署)。
- **有向无环(DAG)**:不能有环,不能让节点依赖自己或下游的后代。Evermind 在后端会严格拓扑排序,成环会被拒。
- **依赖关系要吻合语义**:`builder` 不能依赖 `deployer`,`reviewer` 不能在 `builder` 之前。

## 并行 vs 串行

- **并行节点**(同一拓扑层级)用 `asyncio.gather` 真并发,速度翻倍。典型:2-3 个 `builder` 分别负责不同页面或不同模块。
- **串行节点**:`reviewer → tester → debugger` 通常必须串行,因为 tester 要看 reviewer 的评分,debugger 要看 tester 的报告。
- **推荐 builder 并行数**:2 或 3 个最优。再多会因为 Kimi API 速率限制反而变慢。

## 输入目标的写法技巧

Planner 看你的目标加画布上的节点列表来分派任务。好目标 = 清晰的产出形态 + 关键约束:

- ✅ 好例子:"做一个 3D 射击游戏,第三人称视角,WASD 移动,鼠标拖拽转视角,有关卡和怪物 AI"
- ❌ 差例子:"做个游戏"(太模糊,Planner 只能瞎猜)

如果你选了 3D 游戏模板,同时目标只说"做个简单的 2D 消除",Planner 会以你的目标为准 —— 因为节点骨架可能不匹配任务(比如 3D 模板有 `spritesheet` 但消除游戏不需要)。**最好让模板和目标一致**。

## 常见坑

1. **并行太多 builder 反而慢**:超过 3 个 builder 会撞 Kimi 速率限制,stream 排队。最多 3 个。
2. **reviewer 后面没接 debugger**:reviewer 会 REJECT 质量不达标的产出。如果没有 debugger,REJECT 就直接死。至少要有 `reviewer → debugger → reviewer` 的回环能力(靠 rejection budget)。
3. **imagegen / spritesheet / assetimport 少接就跑不通 3D 游戏**:3D 游戏必须有 `assetimport` 准备 three.js 模型路径,否则 builder 没素材可用。
4. **merger 只有 1 个 builder 时不需要**:只有 1 个 builder,产出就是 `index.html`,不需要 merger;2+ 个 builder 才需要 merger 整合。
5. **自定义节点后 planner 不感知** —— 已在 V5.8.6 修复。现在你的画布节点列表会完整传给 planner,它会严格按你的节点分派 handoff。

## 速度模式 vs 深度模式

设置面板里的 **速度模式**:
- **Fast**:builder timeout 600s,reviewer 允许 1 次 REJECT。**质量普通,适合快速迭代**。
- **Deep**:builder timeout 3600s,reviewer 允许 2 次 REJECT,analyst 做充分调研。**质量高,30-40 min 典型耗时,适合最终交付**。

**推荐**:质量优先就用 Deep,原型验证用 Fast。

## 模板推荐

| 模板 | 适用场景 | 节点数 |
|---|---|---|
| **Quick Landing Page (landing)** | 单页营销站 / 快速原型 | 5 |
| **Web Development (webdev)** | 多页网站 / SaaS 登陆页 | 7 |
| **3D Game Premium (game3d)** | 3D 游戏 / Three.js 项目 | 13 |
| **Data Dashboard (dashboard)** | 数据仪表盘 / 重图表产品 | 10 |
| **Art Asset Pipeline (artpipe)** | 纯美术素材管线 | 7 |
| **Automated Bug Fix (bugfix)** | 对已有项目做自动修 | 8 |
| **Full Stack Pro (fullstack)** | 企业级全栈 | 10 |

## 自己编排时的心法

1. **先想清楚产出形态**:单页 / 多页 / 游戏 / 仪表盘?决定骨架选择。
2. **从最小可行 DAG 开始**:`router → planner → builder → reviewer → deployer` 是最短管线。在这上面加节点,不要一口气堆 13 个。
3. **每加一个节点问自己**:"它的上游能给我什么输入?它的下游需要什么输出?"—— 能答上来才保留。
4. **先跑一轮验证拓扑**:先用 Fast 模式跑一次,看节点是否都按预期启动。拓扑 OK 再切 Deep 跑高质量版本。

有问题随时 `/help` 问 Chat Agent —— 它能看到你当前画布的节点列表,能给针对性建议。
