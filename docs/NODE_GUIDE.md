# Evermind Node Layout Guide

> Minimal mental model for the custom canvas, plus common pitfalls

## 1. Node role catalogue

| Node | Role | Purpose | Typical static upstream |
|---|---|---|---|
| **planner** | Planner | Break down the goal into sub-tasks, define the DAG | (root node, no upstream) |
| **analyst** | Analyst | Online research + technical brief (images / references / sample implementations) | `planner` |
| **uidesign** | UI Designer | Design spec: palette / typography / layout grid | `analyst` |
| **scribe** | Copywriter | Site copy / narrative content | `analyst` |
| **imagegen** | Image generator | Use SD/Comfy to generate game asset images | `analyst` |
| **spritesheet** | Spritesheet | Pack multiple images into a sprite atlas | `imagegen` |
| **assetimport** | Asset importer | Pull web images/videos locally and annotate references | `analyst` or `spritesheet` |
| **builder** (×N) | Builder | Write the actual code (HTML/CSS/JS) | All upstream of `analyst` / `uidesign` etc. |
| **merger** | Merger | Merge ≥2 builder outputs into a unified deliverable | **≥2 builders** |
| **polisher** | Polisher | Tweak motion / typography / whitespace (no structural changes) | `builder` or `merger` |
| **reviewer** | Reviewer | Audit quality in a real Playwright browser | `polisher` / `merger` / `builder` |
| **patcher** | Patcher | **Dynamically triggered** fix path after a reviewer rejection | ⚠️ see below |
| **debugger** | Debugger | Fix runtime errors (JS/DOM errors) | `reviewer` |
| **tester** | Tester | Run interaction tests (click / drag / keyboard) | `reviewer` or `debugger` |
| **deployer** | Deployer | Write the final preview URL, archive the artifact | End of pipeline |

## 2. Correct wiring (golden pipelines)

### Simple website (5 nodes)
```
planner → builder → reviewer → patcher → deployer
```

### Standard website (with UI design)
```
planner → analyst → uidesign → builder → polisher → reviewer → patcher → deployer
```

### High-quality website (dual builders)
```
planner → analyst → uidesign → builder1 ┐
                                builder2 ┴→ merger → polisher → reviewer → patcher → deployer
```

### 3D game (most complex)
```
planner → analyst ──→ imagegen ┐
                              ├→ spritesheet → assetimport ┐
                              builder1 ─────────────────────┤
                              builder2 ─────────────────────┴→ merger → polisher → reviewer → patcher → debugger → deployer
```

## 3. ⚠️ Common pitfalls

### ❌ Mistake 1: drawing a `patcher → reviewer` reverse edge

```
WRONG: reviewer ←→ patcher  (bidirectional deadlock)
RIGHT: reviewer ← patcher    (one-way, patcher depends on reviewer)
```

**Why**: you might think "patcher patches, then reviewer audits again" needs a `patcher → reviewer` reverse edge. **It doesn't.** The orchestrator's v7.10 multi-round loop **dynamically** resets the reviewer to PENDING for re-audit — no static dependency required.

If you do draw a bidirectional edge, v7.41 auto-disconnects the reverse edge and logs a warning, but it's still recommended to draw it one-way only.

### ❌ Mistake 2: merger with only 1 builder

```
WRONG: builder → merger → reviewer        (merger has nothing to merge)
RIGHT: builder1 ┐
       builder2 ┴→ merger → reviewer      (≥2 builders)
```

**Why**: the merger's job is to diff/merge ≥2 peer builders' code. With only 1 builder upstream, merger NOOPs (output_len=59 chars, files=0) — no error, but no value either.

### ❌ Mistake 3: patcher's upstream is not reviewer

```
WRONG: builder → patcher → reviewer       (patcher has no reviewer feedback, blind editing)
RIGHT: builder → reviewer → patcher       (reviewer gives blocking_issues, then patcher knows what to fix)
```

**Why**: patcher is the post-reject fix node. Its upstream MUST be reviewer so it has `blocking_issues` to act on.

### ❌ Mistake 4: deployer before patcher

```
WRONG: builder → reviewer → deployer → patcher    (deploy first, patcher edits won't land)
RIGHT: builder → reviewer → patcher → deployer    (patch first, then deploy the latest version)
```

**Why**: deployer writes the final preview URL — it MUST run after all edits complete.

### ❌ Mistake 5: orphan / dangling nodes

```
WRONG: planner → builder → reviewer
              ↓
              uidesign      (this uidesign has no downstream — it gets stuck when the run finishes)
```

**Why**: every node must lie on a path (except the final deployer). Orphan nodes either stall or get ignored.

### ❌ Mistake 6: missing deployer

```
WRONG: planner → builder → reviewer       (run finishes but the user can't find the output)
RIGHT: planner → builder → reviewer → deployer
```

The deployer produces the "final delivery link" (`http://127.0.0.1:8765/preview/...`). Without it the artifact is on disk but the UI shows no link.

## 4. Call counts / limits

| Node | Default count | Config |
|---|---|---|
| reviewer↔patcher loop ceiling | **3 rounds** | Settings → Reviewer Max Rejections |
| Single patcher's own retry | 1 time (after v7.38; failure triggers reviewer re-audit directly) | Not configurable |
| Builder retries | 3 times (kimi prompt-cache hits help) | Settings → Max Retries |
| analyst source_fetch | 8 times (first round) / 2 times (retry) | Not configurable |

## 5. Best practices

1. **Start from a template**: copy and tweak one of the built-in webdev / fullstack / game3d templates rather than drawing from a blank canvas.
2. **Save before sharing**: v7.39+ persists x/y coordinates, so re-loading keeps the layout intact.
3. **Inspect wiring before run**: make sure every node is on the main path with no islands.
4. **Patcher is a safety net, not mandatory**: simple tasks can skip patcher and deploy directly when reviewer passes.
5. **Multiple reviewers are pointless**: re-auditing the same artifact several times doesn't get stricter; bumping `reviewer_max_rejections` and letting the single reviewer iterate is better.

## 6. When you're stuck

1. **Node stuck for 5+ minutes**: quit and relaunch the app (Cmd+Q then double-click). The `run` state is persistent — restart doesn't lose data.
2. **Node shows "unknown"**: your template version is too old (saved before v7.34) and all node keys are 'agent'. Just save once and you're set.
3. **Reviewer gives 4/10 but patcher makes it worse**: v7.35 auto-detects regressions and rolls back to the higher-scoring previous version (automatically at end-of-run).
4. **Read the backend log**: `tail -f ~/.evermind/logs/evermind-backend.log` shows every node's status transitions live.

---

**Version**: v7.41 (2026-04-29)
**Contributing**: please report new pitfalls or share templates via GitHub Issues.
