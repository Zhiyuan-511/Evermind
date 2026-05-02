# Evermind v7.56 深度调研 + 优化方案报告

**日期**：2026-04-30
**触发**：run_d36f804773d1 失败（"未来科技 3D 网站" 任务，26 分钟产出 0 行 3D 代码）
**目标**：开源前最后一轮系统性优化，确保任务听话度 + 节点闭环正确性

---

## 第一部分：事故全貌

### 1.1 失败 run 时序

```
20:42:12  planner 启动（1014 字 brief，task_type=website len=1014 multi_page=True）
20:42→48  planner+analyst+uidesign+scribe（uidesign+scribe 仍卡 retry，因为 .app 当时还是旧代码）
20:55→56  builder1+builder2 并行 ✅ 双 builder 触发正常
20:57→59  merger + polisher ✅
21:00→02  reviewer round 1 → score 3.62/10 reject
21:02→07  patcher round 1 (657 chars 0 edits BLOCK) → reviewer round 2 reject → patcher round 2 (12739/0) BLOCK → reviewer round 3 reject → patcher round 3 (12817/0) BLOCK
21:07:52  v7.38 budget 3/3 used → failing cleanly → run FAILED
```

### 1.2 产出质量审计

| 文件 | 大小 | 3D 内容 |
|---|---|---|
| index.html | 17741 B | `three.module: 0  WebGLRenderer: 0  scene: 0  camera: 0  <canvas>: 0` |
| about.html | 25300 B | 同上 |
| pricing.html | 11007 B | 同上 |
| platform.html | 28558 B | 同上 |
| features.html | 13728 B | 同上 |
| solutions.html | 9801 B | 同上 |
| faq.html | 23013 B | 同上 |
| contact.html | 21037 B | 同上 |

**结论**：8 个 HTML 共 150KB，**0 行 Three.js / WebGL / 3D canvas 代码**。任务要求 "WebGL 创意开发者 + 3D 沉浸式" 完全没实现。**事实上的废品**。

---

## 第二部分：核心问题诊断

### P0-1：任务分类盲点（致命）

**位置**：`task_classifier.py:1460-1469`（v7.2 修复引入）

```python
_strong_website = re.compile(r"(网站|website|网页|web_page|官网|...)")
if _strong_website.search(text):
    return PROFILES["website"]   # ← 直接返回，永远到不了 creative 分支
```

**症状**：任务 `"未来科技 3D 网站 - WebGL 创意开发者..."` 被分类器判为 `task_type=website`。

**根因**：v7.2 修复是为了防止 "购物网站 + 演示" 被误判为 PPT，但副作用是 **任何含"网站"的任务** 都直接走 plain website 路径，永远到不了下方的 creative 模式判定（含 WebGL/3D/threejs 关键词）。

**影响链**：
1. task_type=website → pro_template 选 design_and_content → builder 走 `website` 套路
2. builder system prompt 没有"必须嵌 Three.js"信号
3. analyst/uidesign 也都按"网站 brief"研究（虽然 uidesign 提到 React Bits 但 React Bits 不是真 3D）
4. → 最终交付 8 页 HTML 没一行 3D

---

### P0-2：Builder 不听话（核心质量问题）

**症状**：builder1 (68K chars 4 files) + builder2 (89K chars 4 files) + merger 都能正常输出 HTML 但**没有任何 3D 代码**。

**根因分析（多重）**：

**(a) Prompt 没强约束**：当前 builder.yaml 没有"任务包含 3D/WebGL 时必须嵌入 Three.js scene"的强制条款。LLM 看到 "网站" 默认理解成"传统响应式网页"。

**(b) System prompt 不带 task-specific 强信号**：现在的 builder system prompt 是按 task_type 选模板。task_type=website → 用普通 website 模板，不知道 brief 里有 "WebGL/3D/沉浸"。

**(c) Analyst 也没识别**：analyst 输出虽长（30K+）但没有"必须真 Three.js 实现"的红线。

**(d) Reviewer 也无能为力**：reviewer 看到产出没 3D 给低分，但 reviewer 的反馈是 "缺少 3D 体验" 这种文字层面的，**patcher 不知道这意味着要插入完整 Three.js scene**。

---

### P0-3：Patcher 三轮全 0 edits 死循环（核心闭环问题）

**症状**：
- Round 1: 657 chars output, 0 edits → v7.11 BLOCK
- Round 2: 12739 chars output, 0 edits → v7.11 BLOCK
- Round 3: 12817 chars output (finish=length 截断), 0 edits → v7.38 budget 用尽 fail

**根因分析（多重）**：

**(a) Reviewer brief 不指示具体修改**：reviewer 输出 1146/2150/926/2876 chars 都是评分文字，没说"在 line N 加 X 代码"这种 actionable 指令。patcher 看不懂"runtime errors"的具体含义。

**(b) Patcher prompt 太抽象**：v7.51 yaml 已经强调 SEARCH/REPLACE 格式，但 LLM 看到 reviewer 的笼统反馈，写不出具体 SEARCH/REPLACE 块。它会写"我建议添加 Three.js"这种 prose，但没有 file_snapshot 里对应的 SEARCH 文本。

**(c) "整体重写"型修复不适合 patcher**：当 reviewer 说"缺 3D 体验"时，patcher 实际需要的是**重写 builder 输出**而不是"surgical edit"。patcher 工具集（SEARCH/REPLACE 块）专为微调设计，不适合从无到有添加 200 行 Three.js 代码。

**(d) v7.10 multi-round 让局面恶化**：每轮 patcher 失败 → reviewer 重审 → 新 reject brief → patcher 又写 prose → ... 死循环 3 轮，浪费 5+ 分钟。

---

### P1-4：uidesign/scribe 27 分钟 retry 浪费

**已知根因（已修但本轮未生效）**：kimi 中转站 stream 偶发 hang + LiteLLM retry 复用同一个 cached OpenAI client → 同一条死 keep-alive 连接 → 必然继续 hang。

**v7.55 P3.4 已修**：retry 时调用 `drop_openai_clients_after_hang()`，强制重建 fresh httpx 连接。但 .app 当时跑的是旧代码，所以本轮还是踩了。

**遗留问题**：
- uidesign.yaml 168 行 / 10K chars 系统提示偏冗长（虽不是 hang 根因，但 LLM 处理负担更大）
- scribe 任务定义模糊（"docs / content / copy"三选一），LLM 容易写出泛泛之谈

---

### P1-5：Reviewer 反馈质量不足

**症状**：reviewer 评分文字（1146-2876 chars）但里面 blocking_issues 数组要么空要么 1-2 项，且文字描述抽象。

**根因**：
- reviewer 系统提示侧重"打分"而非"诊断"
- 没有强制 reviewer 输出 `{file, line, current_text, suggested_fix}` 这种结构化精确指令
- 没有 file_snapshot 让 reviewer 引用具体行号

---

### P2-6：缺乏"任务真实性"门控

**问题**：pipeline 完成时没有"是否真的实现了核心需求"的门控。

举例：
- 任务说"WebGL 3D 网站"
- 产出 0 行 3D 代码
- reviewer 给 3.62/10 reject，但**没有任何节点说"等等，这根本不是 3D"**
- pipeline 跑完 26 分钟后才以"budget 用尽"收尾

**缺少的能力**：
- 任务 brief → 关键能力词提取（WebGL, 3D, particle, shader 等）
- 产出物扫描（HTML 内是否真有这些词出现且非装饰）
- 命中率 < 50% → 强制 builder 重新跑或者标记 "高风险产出"

---

## 第三部分：优化方案（v7.56）

### 修复 1：task_classifier 加 hybrid 识别（P0-1 修复）

**位置**：`task_classifier.py:1460` 之前插入新检查

**逻辑**：
```python
# v7.56: 强 3D/WebGL 网站走 creative_website hybrid（既是网站 multi_page 又含真 3D）
_creative_website_signal = re.compile(
    r"(WebGL|three\.?js|sketchfab|GLSL|GPU\s*shader|"
    r"3D\s*(网站|website|portfolio|页面|场景)|"
    r"沉浸式|immersive|3D\s*体验|raymarching|particle\s*system|"
    r"awwwards.*3D|apple\s*vision|spatial\s*web)",
    re.IGNORECASE,
)
if _creative_website_signal.search(text) and _strong_website.search(text):
    # hybrid: 保留 multi_page 能力但用 creative profile
    profile = PROFILES["creative"].with_multi_page_capability()
    profile.is_3d_required = True  # 新元数据，让 builder 看到
    return profile
```

**衍生工作**：
- 给 PROFILES["creative"] 加 `with_multi_page_capability()` 方法
- 给 TaskProfile 加 `is_3d_required: bool = False` 字段
- pro_template 检测此字段时强制 builder system prompt 加 3D 强制条款

---

### 修复 2：Builder 强 3D 注入（P0-2 修复）

**位置**：`ai_bridge.py` 的 builder system prompt 拼装阶段（搜 `_compose_system_prompt` 或 builder.yaml 引用处）

**注入内容**（条件激活）：
```
[V7.56 强制 3D 真实性约束]

任务包含 WebGL / Three.js / 3D 沉浸式需求。本次产出**必须**：

1. 在 index.html 主页 `<head>` 加载 Three.js: 
   <script type="module" src="https://unpkg.com/three@0.160.0/build/three.module.js"></script>
2. 至少一个 `<canvas id="...">` 元素（推荐 hero 全屏铺满）
3. JavaScript 中必须出现：`new THREE.WebGLRenderer`, `new THREE.Scene`, `new THREE.PerspectiveCamera`, `requestAnimationFrame`
4. 至少一个真实 3D 物体（Mesh + Geometry + Material）持续动画
5. 不允许只用 CSS transform: rotateY / 3D 装饰类伪 3D 替代真 WebGL

如果产出未达到以上 5 项，本次 build 视为失败，reviewer 直接打 0 分，patcher 失败链路启动。
```

---

### 修复 3：Patcher 给"重写"逃生口（P0-3 修复）

当前 patcher 只能输出 SEARCH/REPLACE 微调。但有些 reviewer 反馈本质要求"整段补 100 行 Three.js"——这种用 SEARCH/REPLACE 写不出来。

**方案**：给 patcher 加第二条工具：`file_ops` action="write"（覆盖整个文件）。**但只在 budget round 2+ 启用**，避免第一轮就乱重写。

**位置**：`prompt_templates/patcher.yaml` 加：
```
## v7.56 ROUND ≥ 2 升级: 大块重写权限

如果 reviewer 反馈是 "缺少 X 整块功能"（不是"line N 写错了"这种小修），且
当前已是第 2 轮+，你可以输出**完整文件重写**（用 SEARCH/REPLACE 把整段
旧函数替换为新整段）：

FILE: /tmp/evermind_output/index.html
<<<<<<< SEARCH
<整个旧 <body> 内容>
=======
<完整新 <body> 含 Three.js scene>
>>>>>>> REPLACE
```

并且 `_block_softpass_v754` 给 round 2+ 一些权重——如果输出 ≥ 8000 chars 但 0 SR blocks 且 round=2，**也算合理输出走 SOFT-PASS 不 BLOCK**（因为 LLM 可能在写完整重写但格式不对）。

---

### 修复 4：Reviewer 输出结构化诊断（P1-5 修复）

**位置**：`prompt_templates/reviewer.yaml` 加严格输出契约

```
## v7.56 ACTIONABLE BRIEF FORMAT (mandatory)

输出顶部必须有 JSON：
```json
{
  "verdict": "REJECTED" | "APPROVED",
  "scores": {"functionality": 0-10, "design": 0-10, "originality": 0-10},
  "blocking_issues": [
    {
      "id": "ISSUE-1",
      "severity": "blocker",
      "file": "index.html",
      "anchor_line_range": [120, 145],
      "current_excerpt": "...20 字符...",
      "problem": "缺少 Three.js scene 初始化",
      "suggested_fix": "在 line 145 之后插入完整 init() 函数：包含 WebGLRenderer/Scene/Camera/几何体+动画循环",
      "estimated_lines_to_add": 80
    }
  ]
}
```

每个 blocking_issue **必须**有 anchor_line_range + current_excerpt，让 patcher 能精准定位。
```

---

### 修复 5：uidesign yaml 精简（P1-4 配套）

**变更**：从 168 行 / 10K → 90 行 / 5K

**砍掉**：
- 8 个 palette 库 → 4 个核心（A 极简白 / C 霓虹暗 / G 赛博 / H 编辑大色块）
- v7.0 NEVER 列表（这些应该在 builder 的 guardrails，不是 uidesign 责任）
- React Bits 11 → 6 个最常用

**加入**：
- "如果 brief 含 3D/WebGL/Three.js，Design Tokens 必须含 GPU-friendly 颜色（避免渐变堆叠）"
- "Layout Blueprint 必须明确指出 hero 是否为 Three.js canvas 全屏"

---

### 修复 6：增加任务真实性门控（P2-6 长期）

**新节点**：`reality_check`（在 reviewer 之前跑）

逻辑：
1. 提取 raw_goal 关键能力词（用 task_classifier + LLM 助手）
2. 扫描 OUTPUT_DIR 所有 HTML/JS
3. 计算"关键词命中率" = 命中数 / 期望数
4. 如果 < 50%，向 builder 发"高风险信号"标签，触发 builder 重写一次
5. 如果 < 30%，run 直接 fail（不浪费 reviewer/patcher 时间）

**或者最简版**：把这个检查塞到 reviewer 之前的 `_check_task_completion` 函数里。

---

## 第四部分：执行优先级 + 时间表

### Wave 1（开源前必做，1-2 小时）

| # | 修复 | 文件 | 复杂度 |
|---|---|---|---|
| 1 | task_classifier 3D-website hybrid | task_classifier.py | 中 |
| 2 | Builder 强 3D 注入（条件触发） | ai_bridge.py + builder.yaml | 中 |
| 3 | Patcher round 2+ 整体重写权限 | patcher.yaml | 简单 |
| 4 | Reviewer 结构化 blocking_issues | reviewer.yaml | 简单 |
| 5 | uidesign yaml 精简 10K→5K | uidesign.yaml | 简单 |

### Wave 2（开源后下个迭代）

| # | 修复 |
|---|---|
| 6 | 任务真实性门控节点 reality_check |
| 7 | 节点 prompt cache 优化（kimi cache 探究） |
| 8 | 多模型组合策略（builder 用 deep model + patcher 用 fast） |

---

## 第五部分：验证计划

修完后必须连续 3 轮跑成功才能开源：

### Round 1：保卫萝卜 2D 游戏（asset_heavy_game_parallel）
- 验证：双 builder + merger + spritesheet 全部跑通
- 关键指标：是否有真 game loop（requestAnimationFrame + 怪物移动）

### Round 2：未来科技 3D 网站（v7.56 新 hybrid 路径）
- 验证：task_classifier 识别 3D-website hybrid → builder 注入 3D 强约束 → 真 Three.js 输出
- 关键指标：index.html 含 `WebGLRenderer + Scene + animate()` 关键字

### Round 3：简单工具（calculator/timer，task_type=tool）
- 验证：分类器正确判 tool → tool task hard gate 通过
- 关键指标：≥3 控件 + ≥300 行 JS + 交互能用

3 轮全过 → 重打 DMG → 推 GitHub → 开源。

---

## 第六部分：风险评估

### 高风险

- **修复 2 (3D 注入)** 改变 builder 行为，对**非 3D 任务**可能引入回归。需要在条件触发时仅注入，否则不动 prompt。
- **修复 1 (hybrid 类型)** 改变 task_classifier 路由，对历史模板可能误命中。需保守正则 + 单元测试。

### 低风险

- 修复 3/4/5 都是 prompt 层修改，可快速回滚。

---

## 附：本次未涉及但已修的清单（v7.54+v7.55，需测试验证）

- v7.54 P3.1 short-lazy SOFT-PASS BLOCK
- v7.54 P3.2 round 2+ urgency block 注入 prompt
- v7.54 P3.3 timeout 后 drop_openai_clients_after_hang
- v7.55 P3.4 内部 LiteLLM retry 时 drop clients（修 uidesign/scribe 27min 卡顿）
- v7.55 任务分类一致性（patcher 不再被误识别为 task_type=tool）
- v7.55 v7.1k.3 fallback 也加 BLOCK
- v7.55 双 builder 第三条触发条件（rich_singlepage）
- v7.55 BLOCK 路径友好 UX 消息

---

**等待用户确认后开始 Wave 1 优化。**
