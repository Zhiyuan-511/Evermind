# 自定义模式(Custom Mode)教程

自定义模式让你**完全按照自己在画布上排的节点和边来跑 Pipeline**,不再用系统默认的"极速 / 平衡 / 深度"预设骨架。

## 什么时候用自定义模式?

- 你想用 **3+ 个 builder 并行**(预设最多 2 个)
- 你想跳过某些节点(比如不做 reviewer 直接部署)
- 你有非常特殊的管线需求(比如先 debugger 扫旧 bug,再 planner,再 builder)
- 你在做**架构实验**:把 reviewer → tester → debugger 串成环,让 AI 迭代修复

## 3 步操作

### 步骤 1 — 画好你的 DAG

左上角 "模板" 按钮打开 Template Gallery,选一个最接近你需求的模板作起点,或者干脆空白开始自己拖。

**合法 DAG 规则**:
- 有**入口节点**(通常是 `router` 或 `planner`)
- 有**终点节点**(通常是 `deployer`,也可以是 `reviewer`)
- **有向无环**(DAG,不能有环,不能依赖自己的后代)
- 节点之间用**有向边**连接,边的方向 = 依赖方向

### 步骤 2 — 切到自定义模式

左侧聊天面板顶部有 4 个按钮:**极速 / 平衡 / 深度 / 自定义**。点"自定义"。

- 如果画布节点 ≥ 2 个 → 按钮变**绿色**,下方提示确认
- 如果画布节点 < 2 个 → 按钮会告诉你:"先在画布上拖节点或加载模板"

### 步骤 3 — 在聊天输入目标,发送

发送后系统会:

1. 检测自定义模式 + 读你当前画布
2. 把画布 DAG(节点 + 依赖边)作为 `custom_plan` 发给后端
3. 后端的 Planner 会**严格按照你的节点集合**产出每个节点的任务交接单(handoff)
4. Orchestrator 按拓扑顺序并行执行

系统消息会显示:
> ✓ 自定义模式:严格按画布上的 N 个节点拓扑执行。Planner 会为每个节点生成交接单。

## 可用节点

| 节点 | 作用 |
|---|---|
| `router` | 入口分派 |
| `planner` | 任务拆解 |
| `analyst` | 调研 / 参考站点 |
| `uidesign` | UI 设计稿 |
| `imagegen` | AI 图片生成 |
| `spritesheet` | 2D 精灵表 |
| `assetimport` | 3D 资产管线 |
| `builder` | 写代码(支持并行多个) |
| `merger` | 合并多 builder 的产出 |
| `reviewer` | 质量门(内置浏览器 QA) |
| `tester` | 功能测试 |
| `debugger` | 修 bug |
| `polisher` | 局部打磨 |
| `scribe` | 写文档 |
| `deployer` | 打包 + 预览 |

## 3 个示例 DAG

### 示例 A — 最小管线(快速出稿)

```
router → planner → builder → reviewer → deployer
```

5 个节点,适合单页落地页或极简原型。对应 Template Gallery 里的 **Quick Landing Page** 模板。

### 示例 B — 3 并行 builder(重型 Dashboard)

```
router → planner → analyst → uidesign → (builder1, builder2, builder3) → merger → reviewer → deployer
```

10 个节点,3 个 builder 分别负责不同模块(比如图表组 / 筛选器 / 导出面板)。对应 **Data Dashboard** 模板。

### 示例 C — 3D 游戏顶配

```
router → planner
  ├─ analyst ──┐
  └─ imagegen → spritesheet → assetimport ──┤
                                            ├─ builder1 ─┐
                                            └─ builder2 ─┴→ merger → reviewer
                                                              ├→ tester ──┐
                                                              └→ debugger ─┴→ deployer
```

13 个节点,含完整资产管线 + 双 builder 并行 + 严格 QA。对应 **3D Game Premium** 模板。

## 常见问题

### Q: 自定义模式下发送任务,系统还会自己加减节点吗?

**不会**。自定义模式下,后端 orchestrator 严格按你画布上的节点集合跑。Planner 只生成"每个节点要做什么"的 handoff,不会增删节点。

### Q: 我画错边(有环或死节点)会怎样?

后端拓扑排序时会检测到环并返回错误。前端会提示你修。

### Q: 模板加载后想改怎么办?

模板只是把节点铺到画布上。拖动节点 / 右键删除 / 拉新边都可以。改完后点"自定义"确保是按当前画布执行,而不是按模板原样。

### Q: 能在运行中途改节点吗?

**不能**。运行开始后画布被锁定(节点显示执行状态)。想改要先 Stop,然后调整,再发。

### Q: 自定义模式会比"深度"慢吗?

取决于你的节点数量。13 节点的顶配 3D 游戏管线 ≈ 深度模式耗时(30-40 min)。5 节点的极简管线 ≈ 极速模式耗时(5-10 min)。

## 省 Token 心法(v5.8.6 默认已优化)

1. **少放 builder**:2-3 个最优。再多不会更快,反而速率限制排队。
2. **去掉 speculative 模型竞速**:已默认关闭(`EVERMIND_ENABLE_SPECULATIVE=1` 才开),worker 节点 prompt 直降 50%。
3. **删不需要的节点**:比如纯静态页面不需要 `analyst` / `assetimport` / `tester`。
4. **用 Deep 而非 Fast**:Fast 模式 rejection budget = 1,容易直接死;Deep mode budget = 2,有容错窗口。

## 验证你的 DAG 能不能跑

小技巧:在自定义模式下用简短 dummy goal "测试拓扑" 发送,观察系统消息:
- ✅ 显示"严格按画布上的 N 个节点拓扑执行" → 拓扑合法
- ❌ 显示"拓扑有环 / 节点孤立 / 缺少入口" → 按提示修改画布
- 然后 Stop 即可,没消耗多少 token

## 和 Chat Agent 配合

Chat Agent 能看到你当前画布节点。有疑问可以直接在聊天框问:
- "帮我检查这个 DAG 有没有环"
- "我这 3 个 builder 负责的任务应该怎么分?"
- "是不是缺少 merger?"

他会基于当前画布给针对性建议。
