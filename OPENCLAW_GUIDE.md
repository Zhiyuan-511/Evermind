# OpenClaw × Evermind 使用说明书

## 概述

OpenClaw 是一个 AI 智能体，可以通过 MCP (Model Context Protocol) 协议与 Evermind 进行交互。OpenClaw 能够：

- 🎯 接收用户任务指令
- 📋 自动规划工作节点（Planner → Analyst → Builder → Reviewer → Tester → Deployer）
- ⚡ 调度节点到 Evermind 执行
- 📊 实时监控节点执行进度
- 🧠 在 WebSocket 建连时自动拿到 Evermind 注入的接入包（指南 + MCP 配置 + deep link 模板）

---

## 🔗 打开桌面 App（Deep Link）

Evermind 注册了 `evermind://` 自定义协议。**OpenClaw 或其他工具可以直接打开桌面 App**，无需通过浏览器。

### 用法

| 链接 | 效果 |
|------|------|
| `evermind://` | 打开/激活 Evermind 桌面 App |
| `evermind://run?goal=创建登录页面` | 打开 App 并自动填入目标 |

### 在终端中使用
```bash
# 打开 Evermind App
open evermind://

# 打开 App 并传入目标
open "evermind://run?goal=创建一个登录页面"
```

### 在 OpenClaw 中使用
OpenClaw 可以通过 shell 命令打开 Evermind App：
```
open evermind://
```
而不是打开 `http://localhost:3000/editor`。

---

## 运行模式

### 本地执行 (Local Mode) — 推荐
Evermind 后端自行执行所有节点，OpenClaw 仅负责创建任务和监控进度。

**适用场景**: 日常开发、网站构建、游戏开发等

**使用方式**:
1. 打开桌面 Evermind.app
2. 在右下角切换到 `本地执行` 模式
3. 输入目标或通过 OpenClaw 发送指令
4. 节点自动按 DAG 依赖顺序执行

### OpenClaw 直接模式 (Direct Mode)
OpenClaw 作为独立执行端，直接连接 Evermind WebSocket 接收并执行节点任务。

**适用场景**: 需要 OpenClaw 自身 AI 能力处理节点的高级场景

**使用方式**:
1. 打开桌面 Evermind.app
2. 切换到 `OpenClaw` 运行时
3. 确保 OpenClaw 已通过 WebSocket 连接到 `ws://127.0.0.1:8765/ws`
4. 如果 12 秒内没有 OpenClaw 响应，节点会自动标记为失败

---

## 连接与端口

| 服务 | 端口 | 说明 |
|------|------|------|
| 后端 API | `8765` | Python FastAPI 服务 |
| 前端页面 | `3000` | Next.js 前端 |
| WebSocket | `ws://127.0.0.1:8765/ws` | 实时通信通道 |
| 预览服务 | `8765/preview/` | HTML 产物预览 |

---

## 连接即注入（OpenClaw 必读）

当 OpenClaw 连接到 `ws://127.0.0.1:8765/ws` 时，Evermind 会在首个 `connected` 握手包中附带：

- `openclaw.guide`：完整使用指南文本
- `openclaw.mcp_config`：可直接用于 MCP 客户端的配置 JSON
- `openclaw.guide_url`：`/api/openclaw-guide` 接口地址
- `openclaw.deep_links`：`evermind://` deep link 模板

也就是说，**OpenClaw 无需再猜测如何接入 Evermind**。只要成功建连，就应优先读取 `connected.openclaw` 字段并缓存到自身上下文中。

---

## 节点类型说明

| 节点 | 说明 | 超时 |
|------|------|------|
| **Planner** 规划师 | 分析任务、制定执行计划 | 2 分钟 |
| **Analyst** 分析师 | 研究设计参考、收集需求 | 8 分钟 |
| **Builder** 构建者 | 编写代码、创建产物 | 15 分钟 |
| **Reviewer** 审查员 | 打开浏览器检查产品质量，输出 APPROVED/REJECTED | 7 分钟 |
| **Tester** 测试员 | 运行功能测试、可视化测试 | 6 分钟 |
| **Deployer** 部署者 | 确认文件、提供预览 URL | 6 分钟 |
| **Debugger** 调试者 | 修复 Reviewer/Tester 发现的问题 | 15 分钟 |

---

## 难度模式

### 极速模式 (Simple)
- 3 个节点：Builder → Deployer → Tester
- 适合快速原型和简单任务

### 平衡模式 (Standard)
- 4 个节点：Builder → Reviewer + Deployer → Tester
- Reviewer 会检查代码质量

### 深度模式 (Pro)
- 7+ 个节点：Analyst → **2 个 Builder（并行）** → Reviewer + Deployer → Tester → Debugger
- 两个 Builder 分工合作，各自负责不同部分
- Reviewer 会打回不合格产品（最多打回 2 次，可配置）

---

## 常见问题

### 节点一直显示"排队中"
- **正常情况**: 节点在等待前置依赖完成。例如 Builder 等待 Analyst 完成是正常的。
- **异常情况**: 如果所有前置节点已完成但后续节点仍排队，请检查后端日志。

### 节点失败但不自动重试
- 每个节点最多自动重试 3 次
- Builder 质量门失败也会触发自动重试
- Reviewer 最多可打回 Builder 2 次（可通过 `EVERMIND_REVIEWER_MAX_REJECTIONS` 配置）

### OpenClaw 模式节点超时失败
- 确保 OpenClaw 已通过 WebSocket 连接到 Evermind
- 默认超时 12 秒，可通过 `EVERMIND_OPENCLAW_ACK_TIMEOUT_SEC` 调整
- 如果不需要 OpenClaw 直接执行，请切换到 `本地执行` 模式

### App 和网页不同步
- 桌面 App 和网页共用同一个后端
- 如果前端代码有更新，需要重新构建: `cd frontend && npx next build`
- 然后重新打包: `cd electron && npm run dist`

---

## 配置参数

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `EVERMIND_REVIEWER_MAX_REJECTIONS` | `2` | Reviewer 最大打回次数 |
| `EVERMIND_OPENCLAW_ACK_TIMEOUT_SEC` | `12` | OpenClaw 模式超时时间（秒）|
| `EVERMIND_SUBTASK_TIMEOUT_SEC` | 因节点而异 | 节点执行超时时间（秒）|
| `EVERMIND_MAX_RETRIES` | `3` | 节点最大重试次数 |

---

## API 接口（供 OpenClaw 使用）

### WebSocket 消息格式

**连接握手（Evermind -> OpenClaw / UI）**:
```json
{
  "type": "connected",
  "runtime_id": "rt_xxx",
  "pid": 12345,
  "openclaw": {
    "guide": "# OpenClaw × Evermind 使用说明书 ...",
    "guide_url": "http://127.0.0.1:8765/api/openclaw-guide",
    "ws_url": "ws://127.0.0.1:8765/ws",
    "deep_links": {
      "open_app": "evermind://",
      "run_goal_template": "evermind://run?goal=<urlencoded-goal>"
    },
    "mcp_config": {
      "mcpServers": {
        "evermind": {
          "url": "ws://127.0.0.1:8765/ws",
          "transport": "websocket",
          "description": "Evermind God Mode — Autonomous AI Workflow Orchestrator"
        }
      }
    }
  }
}
```

**创建运行**:
```json
{
  "action": "run_goal",
  "goal": "创建一个登录页面",
  "difficulty": "standard",
  "runtime": "local"
}
```

**节点状态更新（OpenClaw Direct Mode）**:
```json
{
  "action": "openclaw_node_update",
  "nodeExecutionId": "ne_xxx",
  "status": "passed",
  "outputSummary": "任务完成",
  "tokensUsed": 1500,
  "cost": 0.003
}
```

---

## 🔧 技能系统 (Skill System)

Evermind 的技能系统为不同节点提供上下文增强。当 Builder 收到任务时，系统会自动分析目标关键词并加载匹配的技能到 AI 系统提示中。

### 技能分类

| 类别 | 技能示例 | 触发关键词 |
|------|----------|------------|
| **网站/UI** | `commercial-ui-polish`, `responsive-layout-grid` | 官网, 首页, landing page |
| **动画/动效** | `motion-choreography-system`, `lottie-readiness` | 动画, animation, Lottie |
| **游戏** | `godogen-playable-loop`, `gameplay-qa-gate` | 游戏, game, 大战 |
| **演示/PPT** | `slides-story-arc`, `pptx-export-bridge` | PPT, slides, 演示 |
| **文档** | `docs-clarity-architecture` | 文档, documentation, README |
| **图像** | `image-prompt-director`, `svg-illustration-system` | 海报, 封面, 插画 |
| **视频** | `remotion-scene-composer`, `ltx-cinematic-video-blueprint` | 视频, video, 短片 |

### 技能API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/skills` | GET | 列出所有可用技能 |
| `/api/skills/install` | POST | 从 GitHub 安装社区技能 |
| `/api/skills/{name}` | DELETE | 删除已安装的社区技能 |
| `/api/openclaw-guide` | GET | 获取本指南和 MCP 配置 |

### 从 GitHub 安装技能

```json
POST /api/skills/install
{
  "source_url": "https://github.com/user/repo/tree/main/skills/my-skill",
  "name": "my-custom-skill",
  "title": "My Custom Skill",
  "node_types": ["builder"],
  "keywords": ["custom", "optimization"],
  "tags": ["website"]
}
```

---

## ⚡ MCP 一键接入 (Quick-Start Config)

将以下 JSON 粘贴到您的 AI 客户端（Claude Desktop / Cursor / etc.）的 MCP 配置中：

```json
{
  "mcpServers": {
    "evermind": {
      "url": "ws://127.0.0.1:8765/ws",
      "transport": "websocket",
      "description": "Evermind God Mode — Autonomous AI Workflow Orchestrator"
    }
  }
}
```

**步骤：**
1. 复制上方 JSON 配置
2. 打开 AI 客户端的 MCP 设置
3. 粘贴配置并保存
4. 连接成功后，发送任务即可开始使用

---

*最后更新: 2026-03-22*
