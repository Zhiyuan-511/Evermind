# Evermind 内置 QA Preview Session 方案

## 1. 目标

为 `reviewer` / `tester` 增加一条对游戏更友好的内置审查链路：

- 不再默认把游戏 QA 建立在外部浏览器 + 高频截图 + 重视觉分析上
- 在 Evermind 内部直接打开预览并执行试玩
- 自动录制短视频、关键帧、状态日志
- 将证据写入 Evermind artifact store / report store
- 输出结构化 QA 报告，供后续复审和优化使用

这条链路的重点不是“录一个完整视频给模型看”，而是：

1. 内置预览容器负责稳定运行游戏
2. 录屏只作为证据
3. 关键帧、状态变化、错误日志、FPS / 加载耗时等结构化数据才是主分析输入


## 2. 为什么要做

当前本地实现对游戏 QA 明显过重：

- `reviewer` / `tester` 的交互门禁强依赖浏览器动作和视觉证据
- `game` 任务明确要求点击开始、多次按键、可见状态变化
- reviewer/tester 还会走 deterministic visual gate、Playwright smoke、visual regression

相关本地代码：

- 交互门禁：[backend/orchestrator.py](../backend/orchestrator.py)
  - `_interaction_gate_error()` 中 `task_type == "game"` 分支
- reviewer 强制浏览器证据：
  - [backend/orchestrator.py](../backend/orchestrator.py)
- tester 强制浏览器证据：
  - [backend/orchestrator.py](../backend/orchestrator.py)
- Playwright smoke / visual regression：
  - [backend/preview_validation.py](../backend/preview_validation.py)
- 现有内置预览入口：
  - [frontend/src/components/PreviewCenter.tsx](../frontend/src/components/PreviewCenter.tsx)
  - [frontend/src/components/PreviewPane.tsx](../frontend/src/components/PreviewPane.tsx)
  - [frontend/src/app/editor/page.tsx](../frontend/src/app/editor/page.tsx)
- 现有 Electron preload 很薄：
  - [../../../preload.js](../../../preload.js)

结论：

- 问题不是“游戏本身一定卡”
- 问题是“当前 QA 方式对游戏预览不友好”
- 最合理的修复方向是：保留 Evermind 内置预览，给 reviewer/tester 加专用 QA 会话层


## 3. 参考项目与文档

### 3.1 核心参考

1. Playwright
   - GitHub: https://github.com/microsoft/playwright
   - 价值：
     - 成熟稳定的浏览器自动化框架
     - 支持截图、视频、trace
     - 适合做兜底回归和失败时复核

2. Playwright MCP
   - GitHub: https://github.com/microsoft/playwright-mcp
   - 价值：
     - 给 LLM 提供结构化网页交互能力
     - 通过 accessibility snapshot 工作，减少“纯截图驱动”
     - 比 browser-use 更适合作为轻量网页检查层

3. Electron `desktopCapturer`
   - 官方文档: https://www.electronjs.org/docs/latest/api/desktop-capturer
   - 价值：
     - Electron 官方支持桌面 / 窗口视频采集
     - 可配合 `MediaRecorder` 录制 Evermind 内部 QA 窗口

4. Electron `webContents`
   - 官方文档: https://www.electronjs.org/docs/latest/api/web-contents
   - 价值：
     - 支持页面截图 `capturePage`
     - 可做关键帧抓取、失败时快照取证

5. Playwright 视频录制
   - 官方文档: https://playwright.dev/docs/videos
   - 价值：
     - 如果保留外部浏览器兜底链路，可直接产出视频证据

6. Playwright Trace Viewer
   - 官方文档: https://playwright.dev/docs/trace-viewer
   - 价值：
     - 失败场景下非常适合复盘点击、等待、页面状态、console/network

### 3.2 辅助参考

7. Browserless
   - GitHub: https://github.com/browserless/browserless
   - 价值：
     - 如果本地 Chromium 仍拖慢流程，可把浏览器自动化挪到独立服务
     - 更适合“外部浏览器复核层”，不适合作为主游戏 QA 层

8. rrweb
   - GitHub: https://github.com/rrweb-io/rrweb
   - 价值：
     - 适合网页 DOM 记录回放
     - 对普通网站不错
     - 但对 Canvas / WebGL 游戏证据价值有限，不能替代视频 + 状态日志

9. Cap
   - GitHub: https://github.com/CapSoftware/Cap
   - 价值：
     - 不是直接依赖
     - 适合作为“录屏产品交互与文件流”参考

10. Screenpipe
    - GitHub: https://github.com/mediar-ai/screenpipe
    - 价值：
      - 适合作为长程桌面录制 / OCR / timeline 产品参考
      - 不建议直接并入 Evermind 主链，太重

11. Terminator
    - GitHub: https://github.com/mediar-ai/terminator
    - 价值：
      - 提供 OS 级桌面自动化思路
      - 可作为未来“升级桌面审查链”参考
      - 当前阶段不建议作为第一版核心依赖


## 4. 选型结论

### 4.1 推荐主方案

`Evermind 内置 QA Preview Session + Electron desktopCapturer + 结构化状态采样`

### 4.2 推荐兜底方案

`Playwright / Playwright MCP + trace/video`

### 4.3 不推荐作为第一版主链

- `browser-use`
  - 现在的痛点就是 agent 在浏览器里高频观察和操作导致慢
  - 它更像放大器，不是减负器

- `rrweb` 作为游戏主证据
  - 对 DOM 页面价值高
  - 对 WebGL/Canvas 游戏不够

- `Screenpipe` / `Terminator` 直接入主链
  - 工程体量过大
  - 适合后续增强，不适合第一阶段止血


## 5. 总体架构

### 5.1 目标结构

新增一条游戏专用 QA 路由：

1. `builder/deployer` 完成后发出 `preview_ready`
2. `reviewer/tester` 发现 `task_type == game`
3. 不再优先调用普通 browser/browser_use
4. 启动 `QA Preview Session`
5. Evermind 自动切到 QA 预览
6. QA Runner 执行：
   - 加载预览
   - 点击开始
   - 发送按键序列
   - 录制 8-15 秒短视频
   - 保存 3-5 张关键帧
   - 采集 FPS / load time / errors / state changes
7. 后端将视频、关键帧、状态快照、日志写入 artifact store
8. reviewer/tester 基于结构化证据产出报告
9. 仅在失败或证据不足时，升级到 Playwright 兜底复核

### 5.2 关键设计原则

- 主分析输入：
  - 状态日志
  - 关键帧
  - console/page/network 错误
  - FPS / 时间线

- 视频只做：
  - 证据
  - 人工复核
  - 失败回放

- 不让模型直接长时间“看视频再理解”


## 6. 具体实现方案

## 6.1 前端层

### 新增组件

- `frontend/src/components/QAPreviewSession.tsx`
- `frontend/src/hooks/useQASession.ts`

### 职责

- 显示 reviewer/tester 专用 QA 控制条
- 显示当前 QA 状态：
  - loading
  - ready
  - recording
  - interacting
  - analyzing
  - completed
  - failed
- 展示录制时长、关键帧计数、当前步骤
- 提供人工复核入口：
  - 回放视频
  - 查看关键帧
  - 查看状态日志

### UI 行为

- reviewer/tester 节点开始时自动切到 `preview`
- 如果当前 run 是 `game`
  - 预览栏自动进入 `QA Session Mode`
- 不再只是普通 iframe 浏览
- 有一个明确的“QA session badge”


## 6.2 Electron 层

### 新增能力

#### preload

扩展 [../../../preload.js](../../../preload.js)：

- `evermind.qa.startSession(config)`
- `evermind.qa.stopSession()`
- `evermind.qa.captureFrame()`
- `evermind.qa.startRecording()`
- `evermind.qa.stopRecording()`
- `evermind.qa.sendInput(sequence)`
- `evermind.qa.getSessionState()`
- `evermind.qa.onEvent(cb)`

#### main process

新增：

- QA 专用 BrowserWindow 或 WebContentsView
- 录制控制器
- 帧截图器
- 文件落盘器

建议新增文件：

- `electron/qa-session.js` 或项目当前桌面入口旁新增 `qa_session.js`
- `electron/qa_recorder.js`

### 录制实现

首选实现：

- Electron `desktopCapturer`
- renderer 内 `navigator.mediaDevices.getDisplayMedia`
- `MediaRecorder` 输出 `webm`

原因：

- 官方支持
- 实现成本低
- 适合录 Evermind 内部 QA 窗口

补充实现：

- `webContents.capturePage()` 做关键帧
- 每个关键步骤抓 1 张：
  - 初始页
  - 开始后
  - 输入后
  - 结束页


## 6.3 预览页面注入层

### 新增 QA Bridge

在预览页内注入轻量桥：

- `window.__EVERMIND_QA__`

推荐接口：

- `ready(): boolean`
- `startGame(): Promise<boolean>`
- `press(keys: string[]): Promise<void>`
- `snapshotState(): Record<string, unknown>`
- `getMetrics(): { fps?: number; score?: number; lives?: number; level?: number }`
- `markEvent(name: string, payload?: unknown): void`

### 兼容策略

如果游戏实现了 `__EVERMIND_QA__`

- reviewer/tester 直接调用它
- 无需盲点盲按

如果没有实现

- 回退到通用输入脚本：
  - click center/start button
  - Arrow keys / WASD / Space


## 6.4 后端层

### artifact 类型扩展

当前 [backend/task_store.py](../backend/task_store.py) 只支持：

- `browser_trace`
- `browser_capture`
- `state_snapshot`

建议新增：

- `qa_session_video`
- `qa_session_frame`
- `qa_session_log`
- `qa_session_metrics`

### 新增存储内容

- 视频路径
- 帧路径列表
- 状态事件序列
- metrics 摘要
- session metadata
  - run_id
  - node_execution_id
  - preview_url
  - duration
  - task_type
  - started_at / ended_at

### orchestrator 修改

在 [backend/orchestrator.py](../backend/orchestrator.py) 中：

1. `task_type == "game"` 时优先走 `qa_preview_session`
2. 降低普通浏览器门禁优先级
3. reviewer/tester 可以使用：
   - `qa_session_video`
   - `qa_session_frames`
   - `qa_session_state`
   作为主证据
4. 仅在以下情况升级到 Playwright：
   - QA session 启动失败
   - 无状态变化
   - 录制失败
   - 预览崩溃


## 6.5 报告生成层

### reviewer/tester 输出格式

建议报告包含：

- `session_summary`
- `load_result`
- `interaction_result`
- `state_change_result`
- `performance_result`
- `blocking_issues`
- `evidence`
- `recommendation`

### evidence 字段建议

- `video_artifact_id`
- `frame_artifact_ids`
- `state_snapshot_ids`
- `metrics_artifact_id`
- `console_error_count`
- `page_error_count`
- `failed_request_count`

### 结论规则

游戏 reviewer/tester 的通过条件建议至少包括：

- 成功加载
- 成功进入游戏
- 至少 3 次有效输入
- 至少 2 次状态变化
- 无致命 JS error
- 无关键资源加载失败
- 平均 FPS 不低于阈值


## 7. 分阶段实施

## Phase 1：止血版

目标：让 reviewer/tester 不再因为外部浏览器卡顿而完全失效

### 范围

- reviewer/tester 对 `game` 不再强制 `browser_use`
- 新增 `qa_session` 概念，但先不做完整视频分析
- 内置预览中自动：
  - 点击开始
  - 发送固定按键序列
  - 抓 3 张关键帧
  - 写 `state_snapshot`

### 产出

- 可运行的 game QA 快速路径
- artifact store 中能看到关键帧和状态日志
- 报告能引用内置 QA 证据

### 验收

- reviewer/tester 在本机预览里可稳定打开游戏
- 一次 QA 不需要额外打开外部浏览器
- 关键帧与状态日志可保存


## Phase 2：完整录屏版

### 范围

- 加入 `desktopCapturer + MediaRecorder`
- 录制 8-15 秒视频
- artifact types 新增视频类
- 报告显示视频证据

### 验收

- 每次 game reviewer/tester 都能生成短视频
- 视频能在 Task Detail / Reports 里查看
- 失败 case 可回放


## Phase 3：结构化游戏桥接版

### 范围

- 支持 `window.__EVERMIND_QA__`
- 输出分数、生命值、关卡、时间等结构化状态
- reviewer/tester 报告不再只依赖视觉判断

### 验收

- 游戏 QA 报告可包含真实游戏状态
- 误判率明显下降
- review requeue 的建议更具体


## Phase 4：兜底外部复核

### 范围

- 失败时自动触发 Playwright / Playwright MCP
- 保存 trace.zip / 失败视频 / 失败截图

### 验收

- QA session 失败后仍有稳定复核路径
- 可用于定位 preview 层问题和游戏层问题


## 8. Opus 实施任务拆分

## Task A：QA Session 基础设施

负责人：Opus

### 要做

- 扩展 preload API
- 新增 main-process QA session controller
- 新增前端 QAPreviewSession 组件
- reviewer/tester 启动时自动切 preview QA 模式

### 交付

- 可手动启动 / 停止 QA session
- 可发送输入
- 可回传 session 事件


## Task B：关键帧与日志入库

负责人：Opus

### 要做

- artifact type 扩展
- 保存关键帧、状态日志、metrics
- Node Detail / Reports 可查看


## Task C：视频录制

负责人：Opus

### 要做

- `desktopCapturer + MediaRecorder`
- 保存录屏文件
- artifact store / report store 挂接视频


## Task D：orchestrator 路由改造

负责人：Opus

### 要做

- `task_type == game` 时走 QA session 优先分支
- 失败时再回退到 Playwright
- reviewer/tester 输出改用 QA 证据


## Task E：报告模板升级

负责人：Opus

### 要做

- reviewer/tester 统一 QA 报告结构
- 把视频 / 关键帧 / metrics 写入报告


## 9. 我后续 review 的重点

等 Opus 做完后，我会重点检查：

1. 是否真的绕开了当前重浏览器链路
2. 是否把视频仅作为证据，而不是主分析输入
3. artifact store 是否设计干净，没有滥用 `browser_capture`
4. preload / ipc 是否安全，没有把任意文件写入能力暴露给 renderer
5. reviewer/tester 是否仍然保留兜底路径
6. 报告是否能真正帮助 builder/debugger 修复，而不是只生成“看起来专业”的空话


## 10. 风险与规避

### 风险 1：直接在 iframe 上录制不稳定

规避：

- 不把普通 iframe 当最终执行容器
- 做专用 QA session 容器

### 风险 2：视频文件过大

规避：

- 默认 8-15 秒
- 低分辨率
- 关键帧优先

### 风险 3：跨域 / sandbox 导致控制受限

规避：

- Electron 内受控容器
- 必要时改成专用 BrowserWindow / WebContentsView

### 风险 4：模型直接看视频成本高

规避：

- 让模型优先看关键帧 + 状态日志 + metrics

### 风险 5：不同游戏输入方式差异大

规避：

- 先做通用按键脚本
- 再补 `__EVERMIND_QA__` 桥


## 11. 最终建议

最终建议是：

- 第一优先级：做内置 `QA Preview Session`
- 第二优先级：把 `game` reviewer/tester 改成优先走它
- 第三优先级：保留 Playwright 作为失败时复核

不要继续把游戏 QA 主链建立在“普通浏览器 + 高频截图 + 视觉门禁”上。

这条路会让 reviewer/tester 更快、更稳，也更适合你们已经存在的 Evermind 桌面壳。


## 12. 参考链接

- Playwright: https://github.com/microsoft/playwright
- Playwright MCP: https://github.com/microsoft/playwright-mcp
- Browserless: https://github.com/browserless/browserless
- rrweb: https://github.com/rrweb-io/rrweb
- Cap: https://github.com/CapSoftware/Cap
- Screenpipe: https://github.com/mediar-ai/screenpipe
- Terminator: https://github.com/mediar-ai/terminator
- Electron desktopCapturer: https://www.electronjs.org/docs/latest/api/desktop-capturer
- Electron webContents: https://www.electronjs.org/docs/latest/api/web-contents
- Playwright videos: https://playwright.dev/docs/videos
- Playwright trace viewer: https://playwright.dev/docs/trace-viewer
