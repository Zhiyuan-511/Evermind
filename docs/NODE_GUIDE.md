# Evermind 节点排布指南

> 自定义画布的最小心智模型 + 常见坑

## 一、节点角色清单

| 节点 | 中文名 | 作用 | 静态依赖典型上游 |
|---|---|---|---|
| **planner** | 规划师 | 把目标拆成子任务，定义 DAG | （根节点，无上游） |
| **analyst** | 分析师 | 网络调研 + 技术 brief（图片/资料/参考实现） | `planner` |
| **uidesign** | UI 设计 | 设计 spec：配色 / 字体 / 布局 grid | `analyst` |
| **scribe** | 文案 | 网站文案 / 故事性内容 | `analyst` |
| **imagegen** | 图像生成 | 用 SD/Comfy 生成游戏素材图 | `analyst` |
| **spritesheet** | 精灵图 | 把多张图打包成 sprite atlas | `imagegen` |
| **assetimport** | 资产导入 | 把网图/视频抓回本地，标注引用 | `analyst` 或 `spritesheet` |
| **builder** (×N) | 构建者 | 写真正的代码（HTML/CSS/JS） | `analyst`/`uidesign` 等上游全部 |
| **merger** | 合并器 | 把 ≥2 个 builder 的产出合并成统一交付 | **≥2 个 builder** |
| **polisher** | 抛光师 | 微调动效/排版/留白（不改结构） | `builder` 或 `merger` |
| **reviewer** | 审查员 | 用 Playwright 浏览器实地审查质量 | `polisher`/`merger`/`builder` |
| **patcher** | 补丁师 | reviewer 拒绝后**动态触发**修复 | ⚠️ 见下文 |
| **debugger** | 调试器 | 修运行时报错（JS/DOM 错误） | `reviewer` |
| **tester** | 测试员 | 跑交互测试（点击/拖拽/键盘） | `reviewer` 或 `debugger` |
| **deployer** | 部署器 | 写最终 preview URL，归档产物 | 流水线最后 |

## 二、正确连法（黄金管线）

### 简单网站（5 节点）
```
planner → builder → reviewer → patcher → deployer
```

### 标准网站（含 UI 设计）
```
planner → analyst → uidesign → builder → polisher → reviewer → patcher → deployer
```

### 高质量网站（双 builder）
```
planner → analyst → uidesign → builder1 ┐
                                builder2 ┴→ merger → polisher → reviewer → patcher → deployer
```

### 3D 游戏（最复杂）
```
planner → analyst ──→ imagegen ┐
                              ├→ spritesheet → assetimport ┐
                              builder1 ─────────────────────┤
                              builder2 ─────────────────────┴→ merger → polisher → reviewer → patcher → debugger → deployer
```

## 三、⚠️ 常见坑

### ❌ 错 1：patcher → reviewer 的反向连接

```
错: reviewer ←→ patcher  (双向死锁)
对: reviewer ← patcher    (单向，patcher 依赖 reviewer)
```

**原因**：你以为"patcher 修完 reviewer 再审"需要画一条 `patcher → reviewer` 的反向边。**这不需要**！orchestrator 内部 v7.10 多轮闭环会**动态地**把 reviewer 重置为 PENDING 让它重新审查，根本不需要静态依赖表达。

如果你画了双向边，v7.41 会自动断开反向边并 log warning，但还是建议你只画单向。

### ❌ 错 2：merger 只接 1 个 builder

```
错: builder → merger → reviewer        (merger 没东西可合并)
对: builder1 ┐
   builder2 ┴→ merger → reviewer       (≥2 个 builder)
```

**原因**：merger 的工作就是把 ≥2 个 peer builder 的代码 diff/合并。只有 1 个 builder 时 merger 会 NOOP（output_len=59 chars，files=0）— 不报错但也没价值。

### ❌ 错 3：patcher 上游不是 reviewer

```
错: builder → patcher → reviewer       (patcher 没 reviewer 反馈，瞎改)
对: builder → reviewer → patcher       (reviewer 给 blocking_issues 后 patcher 才知道改哪)
```

**原因**：patcher 是 reviewer 拒绝后的修复节点，上游必须是 reviewer 才有 blocking_issues 喂给它。

### ❌ 错 4：deployer 在 patcher 前面

```
错: builder → reviewer → deployer → patcher    (deploy 完了才 patch，patcher 改不进去)
对: builder → reviewer → patcher → deployer    (patch 完了再 deploy 最新版)
```

**原因**：deployer 写最终 preview URL，必须在所有修改完成后才跑。

### ❌ 错 5：孤立节点 / 断头节点

```
错: planner → builder → reviewer
              ↓
              uidesign      (这个 uidesign 没下游，run 完成时它会 stuck)
```

**原因**：每个节点都必须在路径上（除了最后的 deployer）。孤立节点会卡住或被忽略。

### ❌ 错 6：没有 deployer

```
错: planner → builder → reviewer       (run 完成但用户找不到产物)
对: planner → builder → reviewer → deployer
```

deployer 负责生成"最终交付链接"（`http://127.0.0.1:8765/preview/...`）。没它的话产物在磁盘但 UI 没链接。

## 四、调用次数 / 限制

| 节点 | 默认次数 | 配置项 |
|---|---|---|
| reviewer↔patcher 闭环上限 | **3 轮** | Settings → Reviewer Max Rejections |
| 单个 patcher 自身 retry | 1 次（v7.38 后；失败直接触发 reviewer 重审） | 不可配 |
| builder 重试 | 3 次（kimi prompt cache 命中可改善） | Settings → Max Retries |
| analyst source_fetch | 8 次（首轮）/ 2 次（重试） | 不可配 |

## 五、最佳实践

1. **从模板开始**：用内置 webdev / fullstack / game3d 模板复制再改，比从空白画布画安全。
2. **必须保存模板再分享**：v7.39 后保存模板会带 x/y 坐标，重新加载位置不会乱。
3. **Run 前看一眼连线**：确保每个节点都在主路径上，没有孤岛。
4. **patcher 是后悔药，不是必选**：简单任务可以不要 patcher，reviewer 通过就直接 deploy。
5. **多个 reviewer 没意义**：同一个产物连续审查几次，不会更严格；不如增加 `reviewer_max_rejections` 让单 reviewer 多轮工作。

## 六、看不懂时怎么办

1. **节点卡住超过 5 分钟**：退出 .app 重启（Cmd+Q + 双击）。`run` 状态会持久化，重启不会丢。
2. **节点显示 "unknown"**：你的模板版本太老（v7.34 之前保存的），节点 key 全是 'agent'。重新保存一次就好。
3. **reviewer 给 4/10 但 patcher 改完更糟**：v7.35 自动检测到回归会回滚到之前更高分的版本（运行结束时自动）。
4. **看 backend log**：`tail -f ~/.evermind/logs/evermind-backend.log` 实时看每个节点状态变化。

---

**版本**：v7.41 (2026-04-29)
**贡献**：欢迎在 GitHub Issues 报告新坑或提交模板。
