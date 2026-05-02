# Evermind Node Layout Guide

> **The complete reference for the custom canvas** — every node, every edge, every gotcha.

This guide is for users who want to design their own DAG instead of using the built-in Simple/Standard/Pro/Ultra modes. If you just want to use Evermind with a prompt, you don't need to read this — pick a built-in mode and skip to the [README](../README.md).

---

## Table of contents
1. [Mental model in 60 seconds](#1-mental-model-in-60-seconds)
2. [Node role catalogue](#2-node-role-catalogue)
3. [Golden pipelines (copy-paste templates)](#3-golden-pipelines-copy-paste-templates)
4. [Common pitfalls (and why they fail)](#4-common-pitfalls-and-why-they-fail)
5. [Node call counts and budget limits](#5-node-call-counts-and-budget-limits)
6. [Per-node deep dive](#6-per-node-deep-dive)
7. [Edge semantics: static vs dynamic](#7-edge-semantics-static-vs-dynamic)
8. [Best practices](#8-best-practices)
9. [Troubleshooting](#9-troubleshooting)
10. [How the orchestrator actually executes your DAG](#10-how-the-orchestrator-actually-executes-your-dag)

---

## 1. Mental model in 60 seconds

A custom DAG in Evermind is **just a list of nodes connected by edges**. Each node is one of the 14 agent roles below. Each edge means "the downstream node waits for the upstream node to finish, and gets the upstream's output as part of its input context".

The orchestrator does the rest: topological sort, scheduling, retries, model fallbacks, the reviewer ↔ patcher dynamic loop, and a topology reset whenever the reviewer rejects.

**Three rules to internalise:**
- A node only runs **after all its upstream nodes finish successfully**.
- A node's output flows to **all of its downstream nodes** (multi-fan-out is fine).
- The **reviewer ↔ patcher loop is dynamic** — you don't draw it, the orchestrator handles it for you. Just put `patcher` downstream of `reviewer`.

---

## 2. Node role catalogue

| Node | Role | Purpose | Typical static upstream |
|---|---|---|---|
| **planner** | Planner | Decompose the goal into sub-tasks, set the structural blueprint | (root node, no upstream) |
| **analyst** | Analyst | Online research + technical brief (image / reference / sample-impl URLs) | `planner` |
| **uidesign** | UI Designer | Design spec — palette / typography / layout grid — as W3C Design Tokens | `analyst` |
| **scribe** | Copywriter | Site copy + narrative content per page | `analyst` |
| **imagegen** | Image generator | Generate game asset images via image-API (DALL-E / Doubao / Tongyi / SD) | `analyst` |
| **spritesheet** | Spritesheet packer | Pack multiple images into a sprite atlas + JSON map | `imagegen` |
| **assetimport** | Asset importer | Download web images/videos locally and emit attribution manifest | `analyst` or `spritesheet` |
| **builder** (×N) | Builder | Write the actual code (HTML / CSS / JS) | All upstream of `analyst` / `uidesign` etc. |
| **merger** | Merger | Reconcile ≥2 builder outputs into a single coherent deliverable | **≥2 builders** |
| **polisher** | Polisher | Tweak motion / typography / whitespace (no structural changes) | `builder` or `merger` |
| **reviewer** | Reviewer | Audit quality in a real Playwright browser, emit JSON verdict | `polisher` / `merger` / `builder` |
| **patcher** | Patcher | **Dynamically triggered** SEARCH/REPLACE fixer when reviewer rejects | ⚠️ see §7 — must depend on `reviewer` |
| **debugger** | Debugger | Read browser console / runtime errors and propose fixes | `reviewer` |
| **tester** | Tester | Run interaction tests (click / drag / keyboard / form fill) | `reviewer` or `debugger` |
| **deployer** | Deployer | Write the final preview URL, archive the artifact | end of pipeline |

---

## 3. Golden pipelines (copy-paste templates)

These are wired-up examples you can replicate in the canvas. The Pro / Standard / Simple buttons in the UI use these exact graphs.

### Simple website (5 nodes, 3–5 min)
```
planner → builder → reviewer → patcher → deployer
```
*Use for*: counter app, single landing page, MVP.

### Standard website with UI design (8 nodes, 8–15 min)
```
planner → analyst → uidesign → builder → polisher → reviewer → patcher → deployer
```
*Use for*: marketing landing page, simple product site.

### High-quality website with dual builders (10 nodes, 15–25 min)
```
planner → analyst → uidesign → builder1 ┐
                                builder2 ┴→ merger → polisher → reviewer → patcher → deployer
```
*Use for*: multi-page commercial site, portfolio, blog.

### 3D / WebGL game (full pipeline, 30–50 min)
```
planner → analyst ──→ imagegen ┐
                              ├→ spritesheet → assetimport ┐
                              builder1 ─────────────────────┤
                              builder2 ─────────────────────┴→ merger → polisher → reviewer → patcher → debugger → tester → deployer
```
*Use for*: Three.js sites, web games, dashboards with charts.

### Ultra Mode (research-grade, 1–24h)
```
planner → analyst ──→ imagegen ─→ assetimport ─┐
                  └─→ uidesign + scribe        │
                                               ↓
                  builder1 + builder2 + builder3 + builder4 ─→ merger ─→ polisher
                                                                            │
                                                                            ↓
                                                                      reviewer ←──┐
                                                                            │     │
                                                                       (5 rounds) │
                                                                            ↓     │
                                                                         patcher ─┘
                                                                            ↓
                                                              tester → debugger → deployer
```
*Use for*: full mini-app projects, multi-day budgets.

---

## 4. Common pitfalls (and why they fail)

### ❌ Mistake 1: drawing a `patcher → reviewer` reverse edge

```
WRONG: reviewer ←→ patcher    (bidirectional, looks like a deadlock)
RIGHT: reviewer ← patcher     (one-way: patcher depends on reviewer)
```

**Why**: you might think "patcher patches, reviewer audits again" requires a reverse edge. **It doesn't.** The orchestrator's v7.10 multi-round loop **dynamically** resets the reviewer to PENDING for re-audit — no static reverse dependency required.

If you accidentally draw a bidirectional edge, v7.41+ auto-removes the reverse direction and logs a warning. But it's still cleanest to draw it one-way.

### ❌ Mistake 2: merger with only 1 builder upstream

```
WRONG: builder → merger → reviewer       (merger has nothing to merge)
RIGHT: builder1 ┐
       builder2 ┴→ merger → reviewer    (≥2 builders)
```

**Why**: the merger's job is to diff/reconcile ≥2 peer builders' outputs. With only 1 builder upstream the merger NOOPs (output_len ≈ 59 chars, files=0) — no error, but no value either. Pure overhead.

### ❌ Mistake 3: patcher upstream is not reviewer

```
WRONG: builder → patcher → reviewer       (patcher has no reviewer feedback, blind editing)
RIGHT: builder → reviewer → patcher       (reviewer gives blocking_issues, then patcher knows what to fix)
```

**Why**: patcher is the post-rejection fix node. Its upstream MUST be reviewer so it has `blocking_issues` to act on. Without that, the patcher prompt has no actionable input and the LLM just rewrites random parts of the file — usually for the worse.

### ❌ Mistake 4: deployer before patcher

```
WRONG: builder → reviewer → deployer → patcher    (deploy first, patcher edits don't land)
RIGHT: builder → reviewer → patcher → deployer    (patch first, then deploy the latest version)
```

**Why**: deployer writes the final preview URL. It MUST run **after** all edits complete. Otherwise the URL points to the pre-patch version and the user sees stale code.

### ❌ Mistake 5: orphan / dangling nodes

```
WRONG: planner → builder → reviewer
              ↓
              uidesign      (this uidesign has no downstream — it gets stuck when the run finishes)
```

**Why**: every node must lie on a path from `planner` to `deployer` (except `deployer` itself, which is the terminal). Orphan nodes either stall or get ignored.

### ❌ Mistake 6: missing deployer

```
WRONG: planner → builder → reviewer       (run finishes but the user can't find the output)
RIGHT: planner → builder → reviewer → deployer
```

The deployer produces the "final delivery link" (`http://127.0.0.1:8765/preview/...`). Without it the artifact is on disk but the UI shows no link. The user has to manually `open ~/.evermind/workspaces/<task-id>/index.html`, which is a worse UX.

### ❌ Mistake 7: builder without upstream context

```
WRONG: planner → builder → reviewer       (builder gets only the planner blueprint, no design tokens, no copy)
RIGHT: planner → analyst → uidesign → builder → reviewer
```

**Why**: a builder with only the planner blueprint produces a generic page. A builder with `uidesign` (palette/type) and `scribe` (copy) upstream produces something with a specific visual identity. Skip these and you'll get same-looking output across all your runs.

### ❌ Mistake 8: putting `tester` before `deployer`

```
WRONG: builder → reviewer → tester → deployer
                                      ↑ tester runs against the builder output, but then deployer publishes a new copy
RIGHT: builder → reviewer → deployer → tester
                                          ↑ tester runs against the deployed URL, results match what users see
```

**Why**: tester needs a stable URL (`http://127.0.0.1:8765/preview/...`) to drive a Playwright session against. If tester runs before deployer, it's hitting a temporary path.

### ❌ Mistake 9: skipping reviewer

```
WRONG: planner → builder → deployer       (no quality gate, no patcher chance)
RIGHT: planner → builder → reviewer → deployer
```

**Why**: the reviewer is what makes Evermind different from "single agent runs once". Skipping it gets you faster runs but quality regresses to "first-draft LLM". Patcher cannot run without reviewer.

---

## 5. Node call counts and budget limits

| Node | Default count | Where to configure |
|---|---|---|
| reviewer ↔ patcher loop ceiling | **3 rounds** | Settings → Reviewer Reject Budget |
| Single patcher's own internal retry | 1 (after v7.38; failure triggers reviewer re-audit instead) | Not configurable |
| Builder retries (per builder) | 3 | Settings → Max Retries |
| analyst source_fetch (URL crawl) | 8 (first round) / 2 (retry) | Not configurable |
| imagegen images per run | 10 | Settings → Image Generation → Max Images |
| polisher iter cap | 1 | Hardcoded in `orchestrator.py` |
| merger iter cap | 1 | Hardcoded |
| Ultra mode rejection ceiling | 5 | Auto when ULTRA mode enabled |

If your custom DAG hits a limit, the orchestrator emits a `phase=*_BUDGET_EXHAUSTED` event you'll see in `~/.evermind/logs/evermind-backend.log`.

---

## 6. Per-node deep dive

### planner
- **Input**: the user's raw goal + (optionally) chat history
- **Output**: structured markdown blueprint with:
  - `<plan>...</plan>` block (high-level page list, mandatory features, "must avoid" rules)
  - `<builder_1_handoff>` and `<builder_2_handoff>` blocks (≤800 chars each, what each builder should do)
  - `<task_classification>` (game / website / dashboard / etc.)
- **Special**: in v7.57+ the planner blueprint is auto-injected into every downstream code-producing node's system prompt
- **Default model**: `kimi-coding`

### analyst
- **Input**: planner blueprint + user goal
- **Output**: 8K-character technical brief with:
  - Reference URLs (up to 8 fetched, each summarized)
  - Tech stack recommendations
  - Common pitfalls for this category
  - `<node_briefs>` block — per-builder hints
- **Special**: uses real HTTP / browser to fetch reference sites
- **Default model**: `kimi-k2.6-code-preview` (deep mode)

### uidesign
- **Input**: planner brief + analyst tech stack
- **Output**: W3C Design Tokens JSON (palette, type, space, radius, shadow, motion) + layout blueprint + interaction notes
- **Special**: picks one of 4 palette letters (A/B/C/D) deliberately to avoid generic indigo defaults
- **Default model**: `kimi-k2.5` (fast)

### scribe
- **Input**: planner blueprint + analyst brief
- **Output**: page-by-page copy with hierarchy (H1, H2, body, CTA copy, microtext)
- **Default model**: `kimi-k2.5`

### imagegen
- **Input**: analyst brief + image prompts derived from blueprint
- **Output**: PNG/JPEG files in `workspace/images/` + manifest JSON
- **Default**: skipped if no `image_generation.api_key` in Settings; pipeline continues with SVG/CSS placeholders

### spritesheet
- **Input**: image set from imagegen
- **Output**: packed sprite atlas + JSON coordinate map
- **Default**: only runs when builder's task class is `canvas_2d_game` or similar

### assetimport
- **Input**: list of URLs from analyst's reference list
- **Output**: locally cached files + attribution manifest

### builder1 / builder2
- **Input**: planner blueprint + analyst brief + design tokens + copy + (optional) assets
- **Output**: complete HTML/CSS/JS files written to `workspace/`
- **Special**: each builder gets a different `<builder_N_handoff>` slice from the planner so they don't write the same file twice
- **Default model**: `kimi-coding` (slow but high quality for code)

### merger
- **Input**: outputs from builder1 + builder2
- **Output**: unified file tree using SEARCH/REPLACE blocks (preserves common prefix, picks better implementation per file)
- **Special**: detects NOOP cases (identical builder outputs) and short-circuits to direct copy (saves 5–10 minutes of LLM merge time)
- **Default model**: `kimi-coding`

### polisher
- **Input**: merged file tree
- **Output**: SEARCH/REPLACE blocks tweaking motion timing, whitespace, transitions, micro-interactions
- **Hard rule**: must NOT restructure or remove content; only refines
- **Default model**: `kimi-k2.5`

### reviewer
- **Input**: deployed file tree
- **Process**:
  1. Spawns Playwright Chromium pointed at `file://workspace/index.html`
  2. Takes screenshots, scrolls, clicks the primary CTA, captures console logs
  3. Emits structured JSON verdict
- **Output**:
  ```json
  {
    "score": 7.25,
    "verdict": "approve" | "reject",
    "blocking_issues": [...],
    "praise": [...]
  }
  ```
- **Default model**: `kimi-k2.5` *(forced fast-path v7.62 — thinking models don't emit JSON reliably)*

### patcher
- **Input**: reviewer verdict + current file tree (snapshot, ≤40K chars per v7.59)
- **Output**: SEARCH/REPLACE blocks per blocking_issue
- **Hard rule**: NO new features, NO rewrites — must only address the listed issues
- **Default model**: `kimi-coding` (slow code model is fine here, the prompt is structured)

### deployer
- **Input**: latest patched file tree
- **Output**: `http://127.0.0.1:8765/preview/<task_id>/`
- **Behaviour**: deterministic (no LLM call) — just file copy + URL emission

### debugger
- **Input**: console logs + runtime exceptions captured during reviewer/tester
- **Output**: SEARCH/REPLACE patches for runtime errors

### tester
- **Input**: deployed URL + interaction list (every click, drag, form fill)
- **Output**: pass/fail matrix per interaction
- **Default model**: `kimi-k2.5`

---

## 7. Edge semantics: static vs dynamic

There are **two kinds of dependencies** in Evermind, and understanding the distinction is the most important thing in this guide:

### Static dependencies (you draw these)

These are the edges in your canvas. They mean "B must wait for A to finish before B can start". Example: `analyst → uidesign` means uidesign must wait for analyst.

The orchestrator does a topological sort over your static edges. If there's a cycle, the run fails to start.

### Dynamic dependencies (the orchestrator handles these)

The reviewer ↔ patcher loop is **dynamic** — it isn't a static edge you draw. Mechanism:
1. Reviewer runs once, emits verdict.
2. If `verdict == "reject"` and `rejection_count < budget`, the orchestrator:
   - Marks reviewer status = COMPLETED
   - Resets all downstream nodes (deployer, tester, debugger) to PENDING
   - Triggers patcher
   - When patcher finishes, marks reviewer back to PENDING for re-audit
   - Increments rejection_count
3. Loop continues until `verdict == "approve"` OR `rejection_count == budget`.

**You as canvas-author**:
- Draw `reviewer → patcher` (one-way)
- Draw `reviewer → deployer` and/or `reviewer → tester`
- DO NOT draw `patcher → reviewer` (the orchestrator does it dynamically)

This dynamic mechanism is why all downstream nodes always end up running on the patched version, never the pre-patch version.

---

## 8. Best practices

1. **Start from a template**: copy and tweak one of the built-in webdev / fullstack / game3d templates rather than drawing from a blank canvas. Save your tweaks as a new template.
2. **Save before sharing**: v7.39+ persists x/y coordinates, so re-loading keeps your layout intact. Without saving, the next session re-snaps nodes to a default grid.
3. **Inspect wiring before run**: make sure every node is on the main path with no islands. Hover over each node to see its upstream/downstream count — orphans report "0 downstream" except deployer.
4. **Patcher is a safety net, not mandatory**: simple tasks (counter, calculator) can skip patcher. The reviewer will pass on first try, no patcher needed.
5. **Multiple reviewers are pointless**: re-auditing the same artifact several times via parallel reviewers doesn't get stricter. Bumping `reviewer_max_rejections` and letting one reviewer iterate is the right approach.
6. **Don't put `analyst` on simple tasks**: analyst takes 1–4 minutes and is overkill for "build a counter". Use the Simple-mode pipeline for trivial work.
7. **Multiple builders without merger is a waste**: if you have builder1 + builder2 but no merger downstream, the second builder's output is ignored. Always merge.
8. **Polisher only on visual-heavy tasks**: skip polisher for dashboards / data-heavy work; it doesn't add value and burns tokens.
9. **Test your DAG with a small prompt first**: before running an Ultra-grade brief through your custom DAG, validate the topology with a 1-line prompt. Saves you from finding edge bugs at minute 30.
10. **Keep `imagegen` opt-in**: only wire it in if your task class is game / asset-heavy. Otherwise the LLM-generated image API costs add up.

---

## 9. Troubleshooting

### Node stuck for 5+ minutes
Quit and relaunch the app (Cmd+Q + double-click). The `run` state is persistent — restart doesn't lose data. The stuck node will resume or move to the next state on relaunch.

### Node shows "unknown"
Your template version is too old (saved before v7.34). All node keys were stored as 'agent' back then. Just save the template once and the issue is gone.

### Reviewer gives 4/10 but patcher makes it worse
v7.35 auto-detects regressions. At end-of-run, if the latest version scored worse than an earlier snapshot, the orchestrator rolls back to the better version. You'll see a `regression_rollback` event in the run log.

### "Merger took 7 minutes for a tiny change"
This is the v7.49 / v7.66 fix path. If both builders produced near-identical output, merger should NOOP-shortcut (millisecond response) instead of LLM-merging. If you're seeing 7+ minutes, file an Issue with the run ID.

### "Reviewer keeps timing out"
The reviewer's Playwright Chromium has a 90-second budget per audit. If your generated artifact has heavy animation (particles + WebGL), the page might not stabilize. Try Standard mode (no reviewer loop) for now, or bump the timeout via env var (advanced).

### Read the backend log
`tail -f ~/.evermind/logs/evermind-backend.log` shows every node's status transitions live. Each node logs `[node_key] phase=PHASE status=STATUS` on every transition. This is your primary debugging tool.

---

## 10. How the orchestrator actually executes your DAG

For the curious — what `backend/orchestrator.py` does when you click Run:

1. **Topological sort**: builds a DAG from your canvas edges. Detects cycles → run fails to start.
2. **Resource allocation**: each node gets a fresh asyncio task; parallel siblings run concurrently.
3. **Per-node lifecycle**:
   - `PENDING` → `STARTING` → `RUNNING` → `COMPLETED` | `FAILED`
   - On `FAILED`: retry up to `max_retries` (default 3); if still failing, propagate failure and mark all downstream as `FAILED_BY_PARENT`.
4. **Reviewer reject handling**:
   - When reviewer emits `verdict=reject` with `rejection_count < budget`:
     - All downstream nodes (deployer, tester, debugger) reset to `PENDING`
     - Patcher (if present) triggers
     - On patcher COMPLETED, reviewer reset to `PENDING`, re-runs
     - rejection_count incremented
   - When reviewer emits `verdict=approve` OR `rejection_count == budget`:
     - Pipeline continues normally
5. **Per-node timeout**: each node has a per-role timeout (planner 90s, analyst 720s deep / 360s standard, builder 480s base, etc.). On timeout: marked FAILED, retry triggers.
6. **Model fallback**: each node has a model fallback chain (e.g. `[kimi-coding, kimi-k2.5, deepseek-v3]`). On model 5xx / 429, falls through to next.
7. **Ev event stream**: every state change publishes a WS event to the frontend. The canvas updates in real-time.
8. **End-of-run finalization**: writes summary, archives full output to `~/.evermind/workspaces/<task_id>/`, emits preview URL.

If you want to dig deeper, the relevant code is in `backend/orchestrator.py`:
- `Orchestrator._execute_node()` — single-node lifecycle
- `Orchestrator._handle_reviewer_reject()` — the dynamic loop trigger
- `Orchestrator._topology_reset_on_reviewer_reject()` — downstream PENDING reset
- `Orchestrator._run_to_completion()` — top-level driver

---

**Version**: v7.62 (2026-05-01)
**Contributing**: please report new pitfalls or share templates via GitHub Issues.
