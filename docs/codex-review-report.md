# Codex QA Preview Session 实现审查报告

> **审查人**: Opus (Antigravity)
> **审查日期**: 2026-03-28
> **审查范围**: Codex 基于 `docs/qa-preview-session-plan.md` 的全部实现

---

## 1. 改动总览

Codex 一共修改了 3 个文件：

| 文件 | 改动量 | 核心改动 |
|------|--------|----------|
| [ai_bridge.py](file:///path/to/evermind/backend/ai_bridge.py) | ~600 行 | browser_use 集成、analyst 研究门禁、builder direct_text 模式、stream 活性追踪重写、salvage 选择器 |
| [orchestrator.py](file:///path/to/evermind/backend/orchestrator.py) | ~400 行 | 结构化 HTML 解析器、builder 降级保护、refinement context、3D 资产分类、game reviewer/tester 门禁升级 |
| [settings.py](file:///path/to/evermind/backend/settings.py) | 3 行 | 新增 browser_use 配置项 |

---

## 2. 与方案文档 (qa-preview-session-plan.md) 的对照

方案文档提出 **4 个阶段**。Codex 的实现覆盖度：

| 方案阶段 | 是否实现 | 质量评估 | 备注 |
|----------|----------|----------|------|
| Phase 1: 止血版 — 游戏 QA 不再强制浏览器 | ✅ 部分 | ⚠️ 方向有偏差 | 方案要求"内置 QA Session"，Codex 加的是 browser_use 而非 Electron 内置预览 |
| Phase 2: 视频录制 | ❌ | — | desktopCapturer/MediaRecorder 完全未实现 |
| Phase 3: 结构化桥接 `__EVERMIND_QA__` | ❌ | — | 预览注入层完全未实现 |
| Phase 4: Playwright 兜底 | ❌ | — | 尚未触及 |

> [!IMPORTANT]
> **方案的核心主张是**：用 Electron 内置 QA 预览容器替代外部浏览器链路。Codex 做的是在现有 browser/browser_use 之上增强，**方向上是加强了外部浏览器依赖**而非减弱。这是方案审查的最大分歧点。
>
> **但这也是合理的 Phase 0 止血选择** — 在 Electron QA Session 完整落地之前，先让现有 browser_use 管线对游戏更智能、更可靠。

---

## 3. 代码质量审查：✅ 做得好的部分

### 3.1 结构化 HTML 内容完整性解析器 ⭐⭐⭐

```python
# orchestrator.py — _html_content_completeness_stats()
```

**设计亮点**：
- 用 `HTMLParser` 替代了危险的 regex `<(section|div)...>(.*?)</\1>` — 旧 regex 在嵌套 DOM 上完全失效
- 正确排除了 HUD/FX/overlay 等 utility 容器（`aria-hidden`, `position:absolute`, class 名匹配）
- 对中文和英文内容分别设了最低有效长度阈值
- 信号标签（canvas, svg, button, input 等）被视为非空

**影响**：之前游戏 HTML 经常被误判为"empty containers"导致 score 被扣 25 分。这个修复直接解决了 **score=56 → quality gate fail** 的核心问题。

### 3.2 `_select_builder_salvage_text()` 优先级选择器 ⭐⭐

逻辑清晰：recovered_html 如果 body 信号得分更高或字符数超过 135%，优先采用。避免了之前"latest_stream_text 只有 CSS 前缀，recovered_html 有完整 body"时选错源的问题。

### 3.3 `_await_stream_call` 全面重写 ⭐⭐⭐

Codex 完全替换了我之前的简单 `wait_for + 延长` 方案，改为：
- 1 秒粒度的轮询循环
- 追踪 `meaningful_stream_activity_at` 和 `stream_has_meaningful_activity`
- 当 prewrite deadline 到期时，如果 stream 仍有"有意义的"活动（HTML tags, tool calls），自动取消 prewrite deadline 而非立即 timeout
- 这比我原来的 "一次性延长到 hard ceiling" 策略更精细，避免了 dead stream 浪费全部 hard timeout

### 3.4 3D 资产流水线分类 ⭐

```python
# task_classifier.py — game_asset_pipeline_mode()
```

对 3D/Voxel/Minecraft 类游戏返回 `"3d"`，让 imagegen、spritesheet、assetimport 节点使用 3D 专用 prompt 模板，而不是强行用 2D sprite atlas 模板。

### 3.5 Builder refinement context ⭐⭐

当 builder 重试时，从已有的 `index.html` 提取标题、质量分数、heading 列表、内容快照，注入给 builder，引导它"在现有基础上改进"而不是"从零重建"。

### 3.6 Analyst 研究门禁 ⭐⭐

```python
# ai_bridge.py — _analyst_browser_followup_reason()
# orchestrator.py — analyst_reference_gate_failed
```

要求 analyst 至少浏览 2 个不同的 URL 并在报告中列出。之前 analyst 经常用内置知识写一份空洞的设计简报，没有实际查阅参考资源。

---

## 4. 代码质量审查：🔴 需要修复的问题

### Bug 1: `_force_builder_text_timeout_fallback` nonlocal 变量缺失 `tool_call_stats`

```python
# ai_bridge.py ~L4229
async def _force_builder_text_timeout_fallback(reason: str) -> Optional[Dict[str, Any]]:
    nonlocal builder_has_written_file, tool_results  # ← 缺少 tool_call_stats
```

我之前的 Fix C (auto-save in salvage) 在这个函数内部修改 `tool_call_stats`，但 Codex 没有把 `tool_call_stats` 加入 nonlocal。**现在 Python 会默认把它当闭包变量的读引用**，dict 的 `.get()` 可以工作但 `tool_call_stats[key]` 赋值可能会在某些作用域下出问题。

> **风险等级**: 中等 — dict 是可变对象，闭包内修改通常不需要 nonlocal，但代码意图不够明确。

### Bug 2: `timeout_value = hard_timeout = 0` 初始化问题

```python
# ai_bridge.py L4369
timeout_value = hard_timeout = 0
```

`hard_timeout` 在 L4376 被正确赋值为 `timeout_sec`，但如果 L4376 之前出异常（理论上不会），`hard_deadline = started_at + 0` 会立即触发超时。**代码安全但初始化不干净**。

### Bug 3: `_maybe_seed_qa_browser_use` 缺少超时保护

```python
# ai_bridge.py L4611+
result = await self._run_plugin(
    "browser_use", params, plugins, node_type=node_type, node=node,
)
```

`_run_plugin` 调用 `BrowserUsePlugin.execute()`，其内部用 `asyncio.wait_for` 设 180s timeout。但这个 prefetch 发生在 **reviewer/tester 的 API 调用之前**，**同步地消耗 reviewer 节点的 timeout 预算**。如果 browser_use runner hang 了 150 秒，reviewer 的实际 API 调用时间就被压缩到只剩 30 秒。

> [!WARNING]
> **这是最大的计时风险**。建议：
> 1. 给 prefetch 加独立超时 `asyncio.wait_for(..., timeout=60)`
> 2. 或者在 orchestrator 层给 reviewer/tester 在有 browser_use prefetch 时增加 timeout

### Bug 4: builder direct_text 模式下 `_extract_html_files_from_text_output` 的 fallback 可能截取到 system prompt

在 `_extract_html_files_from_text_output` 的 fallback 路径中：

```python
if not files:
    stripped = text.strip()
    if '<html' in stripped_lower or '<!doctype' in stripped_lower) and '<body' in stripped_lower:
        files["index.html"] = stripped
```

如果 `output_text` 包含 system prompt 泄漏（某些模型会回显 system prompt），这里会把 system prompt + HTML 一起提取。应该先 strip 掉 markdown fences 和前导文字。

### Bug 5: `_html_container_is_utility_shell` 的 regex 过度匹配 "background"

```python
r"background|canvas"  # ← 会匹配任何 class 含 "background" 或 "canvas" 的容器
```

`canvas` 在游戏 HTML 中是核心元素，但这里把包含 `canvas` class 名的 div 标记为 utility shell 并跳过。如果 builder 写了 `<div class="game-canvas-wrapper">...</div>`，它会被错误排除，导致内容计数不准。

> **建议**：将 `canvas` 从 utility shell regex 中移除，或者更精确地匹配 `canvas-overlay` 等。

### Bug 6: `_builder_execution_direct_text_mode` 在 orchestrator 中可能与 ai_bridge 的 `_builder_should_auto_direct_text` 判断不一致

```python
# orchestrator.py
def _builder_execution_direct_text_mode(self, plan, subtask):
    # ... checks task_classifier.classify(plan.goal).task_type == "game"
    # ... checks len(assigned_targets) <= 1

# ai_bridge.py
def _builder_should_auto_direct_text(self, node_type, *, ...):
    # ... checks provider == "kimi"
    # ... checks task_classifier.classify(text).task_type == "game"
```

orchestrator 不检查 provider，ai_bridge 不检查 `assigned_targets`。两个函数对同一 flag 可能产生不同判断。当 orchestrator 设了 `builder_delivery_mode = "direct_text"` 但模型不是 Kimi 时，ai_bridge 的 `auto_builder_direct_text` 是 False 但 `force_builder_direct_text` 是 True（因为来自 node dict）。**这看起来是有意的设计**（orchestrator 决定模式，ai_bridge 只做自动检测），但需要确认。

### Bug 7: conversation history 过滤可能误杀有价值的 agent 消息

```python
# orchestrator.py L6337+
if role == "agent" and re.search(
    r"(traceback|stack trace|timed out|timeout|reviewer .* failed|...)",
    content, re.IGNORECASE,
):
    continue
```

这个过滤太激进 — 如果 agent 消息中提到 "The previous attempt timed out, so this retry..."，这其实是有价值的上下文信息，不应该被过滤掉。**会导致 conversation history 丢失重要上下文**。

### Bug 8: `_builder_retry_regression_reasons` 在 stable_preview 不存在时 silently returns empty

这是正确行为但应该有 debug log，因为第一次运行没有 stable preview 时所有 regression 检查都被跳过。

---

## 5. 优化建议

### 5.1 browser_use prefetch 应该做成可取消的后台任务

当前实现是同步等待完成再开始 LLM 调用。更好的做法：
1. 发起 browser_use prefetch 作为后台 task
2. 立即开始 LLM 调用
3. 在 LLM 输出 "no tool calls" 阶段（followup 检查时），await prefetch 结果
4. 这样 browser_use 的 150s 和 LLM 调用并行，而不是串行

### 5.2 `_html_content_completeness_stats` 应该处理 `<script>` 和 `<style>` 内容

当前 HTMLParser 会把 `<style>` 和 `<script>` 标签内的文本也累计到父容器的 `text_parts`。比如：

```html
<div class="game-container">
  <style>.player { color: red; }</style>  <!-- 这个文本会被计入 div 的 text -->
</div>
```

这会导致 CSS 文本被错误地认为是"有意义的文本"。应该过滤掉 `<style>` 和 `<script>` 内的文本。

### 5.3 builder prompt 注入两处重复的 "first-write" 指令

```python
# ai_bridge.py L206-208 — builder 输出格式 prompt #17-19
"17. For game/dashboard/tool/..., your FIRST successful write must already contain visible <body> content..."
"18. For game tasks, the first saved HTML must already render a playable shell..."
"19. Keep the first-pass CSS concise and functional..."

# task_classifier.py L719-728 — first_write_contract
"- Your first successful write must already contain visible <body> content..."
"- For games, the first saved HTML must already render a playable shell..."
```

**两套完全重复的 first-write 指令**分别通过 ai_bridge 的静态 prompt 和 task_classifier 的动态 prompt 注入。应该只保留一处。

### 5.4 `_browser_use_action_events` 方法太长（~160 行）

这个方法做了 4 件事：
1. 解析初始 snapshot
2. 归一化 action names
3. 从 history_items 提取事件
4. 生成 fallback 事件

建议拆成 4 个小方法。

### 5.5 tester 的 game 门禁提到了 "characterss/models/enemies/props/materials/UI mounts look coherent"

```python
"g. Inspect whether characters/models/enemies/props/materials/UI mounts look coherent instead of placeholder-grade\n"
```

对于 HTML5 Canvas 游戏，tester 无法通过 DOM 快照检查 Canvas 内的渲染质量。这个门禁只对 DOM-based 游戏有意义。

### 5.6 analyst retry 策略从 "禁止浏览器" 改为 "必须浏览器" — 矛盾

旧代码：
```python
"⚠️ PREVIOUS ATTEMPT TIMED OUT — DO NOT USE BROWSER THIS TIME.\n"
```

新代码：
```python
"⚠️ PREVIOUS ATTEMPT TIMED OUT.\n"
"This retry MUST still use the browser tool on at least 2 different source URLs..."
```

如果 analyst 因为浏览器超时失败了，重试时强制它再次浏览器... **会再次超时**。原来"不用浏览器"的策略更合理。

> [!CAUTION]
> 建议恢复"timeout 重试不用浏览器"逻辑，或者至少给重试一个更短的浏览器超时。

---

## 6. 架构评审

### 6.1 方案 vs 实现的偏差分析

方案核心主张：**内置 QA Preview Session**（Electron desktopCapturer + preload API + 专用 QA 容器）

Codex 实际做的：**增强现有 browser_use 管线**（prefetch + followup 门禁 + event 归一化）

**评价**：这不是方案失败，而是**合理的 Phase -1 止血**。在完整的 Electron QA Session 落地之前，先让 browser_use 对游戏更智能是务实的。但后续 Phase 1-4 仍然需要做。

### 6.2 `qa_enable_browser_use` 配置的实际可用性

目前 `browser_use` 插件要求：
1. `OPENAI_API_KEY` 或 `EVERMIND_BROWSER_USE_PYTHON` 配置
2. `browser_use` Python 包安装在 sidecar venv
3. runner script 存在

**大多数用户的 Evermind 不满足这些条件**，所以 `_qa_browser_use_required` 几乎总是返回 False。这意味着大部分 Codex 的 browser_use 集成代码目前是 dead code。

### 6.3 builder direct_text 模式的风险

对 Kimi + game 自动切换到 direct_text 模式（不走 tool loop，直接返回 HTML 代码块），这**绕过了 file_ops 的所有安全守卫**：
- HTML 完整性检查（`_validate_deliverable_html_write`）
- 占位符写入拦截（`_guard_deliverable_placeholder_write`）
- HTML 回归检查（`_guard_builder_html_regression`）

唯一的保护是 auto-save 后的 quality gate，但那时候文件已经写入磁盘了。

### 6.4 质量门禁分散在多处的维护风险

现在游戏 reviewer 质量门禁分布在：
1. `ai_bridge.py` — `_review_browser_followup_reason()` (L2680-2830)
2. `ai_bridge.py` — `_maybe_seed_qa_browser_use()` (L2611-2670)
3. `orchestrator.py` — `_interaction_gate_error()` 的 game 分支
4. `orchestrator.py` — reviewer visual gate (browser_calls < 1)
5. `orchestrator.py` — reviewer gameplay gate (browser_use_calls < 1)
6. `orchestrator.py` — tester visual gate / gameplay gate

6 处不同的检查点，5 个不同的 error message 格式。应该集中管理。

---

## 7. 已经由 Opus 补的修复

在此次审查中，以下问题已在之前的会话中修复：

| Fix | 描述 | 状态 |
|-----|------|------|
| Fix 1 | max_tokens 8192 → 16384 + retry 递增 | ✅ 已部署 |
| Fix 2 | json_repair + regex 恢复截断 HTML | ✅ 已部署 |
| Fix 3 | salvage 优先使用 tool args | ✅ 被 Codex 的 `_select_builder_salvage_text` 升级 |
| Fix 4 | 收紧非 builder 节点超时 | ✅ 已部署 |
| Fix 5 | prewrite timeout extension | ✅ 被 Codex 全面重写，更好 |
| Fix 6 | auto-save text-mode HTML | ✅ 已部署，与 Codex 和谐共存 |

---

## 8. 建议的修复优先级

| 优先级 | 问题 | 修复建议 |
|--------|------|----------|
| P0 | Bug 3: browser_use prefetch 无独立超时 | 给 `_maybe_seed_qa_browser_use` 加 `asyncio.wait_for(…, timeout=60)` 包装 |
| P0 | Bug 5: utility shell regex 匹配 "canvas" | 从 regex 中删除 `canvas`，或改为 `canvas-overlay` |
| P1 | 优化 5.2: HTMLParser 不过滤 style/script 文本 | 给 parser 加 `_skip_tags = {"style", "script"}` 逻辑 |
| P1 | 优化 5.6: analyst timeout retry 强制浏览器 | 恢复 "timeout → no browser" 逻辑 |
| P2 | Bug 1: nonlocal tool_call_stats | 添加注释说明 dict mutation 不需要 nonlocal (clarification) |
| P2 | Bug 7: conversation history 过滤太激进 | 改为只过滤 traceback/stack trace，保留 "timed out" 等上下文 |
| P2 | 优化 5.3: 重复的 first-write 指令 | 删除 ai_bridge.py L206-208 的重复行 |
| P3 | 优化 5.1: browser_use prefetch 并行化 | 改为后台 task 与 LLM 调用并行 |

---

## 9. 编译验证

```
✅ ai_bridge.py compiles OK
✅ orchestrator.py compiles OK
✅ settings.py compiles OK
```

所有修改编译通过，无语法错误。

---

## 10. 总结

**Codex 的工作质量整体很高**，特别是：
- 结构化 HTML 解析器替代危险 regex — 直接解决了游戏质量门禁误判
- Stream 活性跟踪重写 — 比我原来的方案更精细更可靠
- Builder refinement context — 防止 retry 从零重建覆盖已有成果
- 回归保护 — 检测 retry 是否比 stable preview 缩水

**主要风险点**：
1. browser_use prefetch 可能吃掉 reviewer 的 timeout 预算（P0）
2. "canvas" class 被错误识别为 utility shell（P0）
3. analyst timeout retry 强制浏览器可能死循环（P1）
4. 方案层面偏离：做的是 browser_use 增强而非 Electron 内置 QA Session

**建议 Codex 下一步**：
1. 修掉 P0/P1 bugs
2. 写单元测试覆盖 `_html_content_completeness_stats` 和 `_select_builder_salvage_text`
3. 考虑 Phase 1 的 Electron QA Session 设计
