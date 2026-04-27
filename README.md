<div align="center">
  <h1>Evermind</h1>

  <p><strong>An open-source multi-agent orchestration desktop app, with a built-in AI browser and native support for every major LLM — local or cloud, Chinese or Western.</strong></p>

  <p>
    <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-blue.svg"></a>
    <a href="https://github.com/Zhiyuan-511/Evermind/releases"><img alt="Release" src="https://img.shields.io/github/v/release/Zhiyuan-511/Evermind?include_prereleases"></a>
    <a href="https://github.com/Zhiyuan-511/Evermind/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/Zhiyuan-511/Evermind?style=social"></a>
  </p>

  <p>
    <a href="https://github.com/Zhiyuan-511/Evermind/releases/latest">Download</a> ·
    <a href="INSTALL.md">Install guide</a> ·
    <a href="BUILD.md">Build from source</a> ·
    <a href="CHANGELOG.md">Changelog</a>
  </p>
</div>

---

## What is Evermind?

Evermind is a desktop app that runs **multiple AI agents as a DAG pipeline** on your Mac.
Compose a router + planner + analyst + parallel builders + reviewer + tester as a graph — not a chain — and watch them collaborate live.

It ships with:

- A **built-in Chromium browser** agents can drive, with a visible AI cursor, click ripples, scroll glow and typing overlays so you can see what the AI is doing.
- A **Monaco code editor** side-by-side with chat.
- **Provider plugins for 12 LLM vendors** — Kimi, Qwen, DeepSeek, Zhipu GLM, Doubao, MiniMax, OpenAI, Anthropic, Gemini, xAI Grok, Mistral, Meta Llama.
- A **quality-guard runtime** that detects narration-only output, empty batches, reasoning-mode leaks and auto-falls-back to tool-call mode.
- **Streaming-everything** WebSocket runtime (port 8765), live trace viewer, persistent session memory.
- **BYOK** — your API keys stay on disk (AES-encrypted) and requests go directly to the provider you configured. No Evermind cloud. No proxy.

> This whole project was written by a human and Claude, together, with zero lines of hand-written code from the author. It's an experiment in what two people — one silicon, one not — can ship in a living codebase.

## Key features

- 🧩 **Multi-agent DAG pipelines** — router / planner / analyst / imagegen / spritesheet / assetimport / builder×N / merger / reviewer / tester / debugger / deployer
- 🌐 **Built-in agent browser** — AX + paint-order snapshot, 13 atomic mouse/keyboard actions, AI cursor overlay, click ripples, scroll edge-glow, typed-char float-up, element highlight boxes
- 📝 **YAML harness templates** — 16-node prompts externalized; hot-swap without rebuild
- 🔌 **12 LLM providers** — one plugin per vendor, not one size fits all
- 🇨🇳 **Kimi / Qwen / DeepSeek / GLM / Doubao / MiniMax** — first-class Chinese LLM support with per-vendor thinking-disable, prompt-cache, retry policy
- 🤖 **OpenAI / Anthropic / Gemini / Grok / Mistral / Llama** — native Responses / Messages / generateContent protocol shaping
- 🖥️ **Monaco editor** — same engine as VS Code, side-by-side with chat
- 🧠 **Session memory** — cross-conversation local SQLite
- 🛡️ **Quality guard** — narration-guard, empty-batch detection (10 KB threshold), kill-switch override — see [V5.8.7](CHANGELOG.md#580-2026-04-18)
- 🎯 **Speculative execution** — parallel peer agents for 2–4× faster pipelines
- 📊 **Live trace viewer** — every tool call, every token, real time
- 🔑 **BYOK + encrypted key storage** — AES-256 on disk
- 🔄 **WebSocket streaming everything** — no 30s stalls

## Quick start

```bash
# macOS Apple Silicon
curl -LO https://github.com/Zhiyuan-511/Evermind/releases/latest/download/Evermind-3.0.0-arm64.dmg
open Evermind-3.0.0-arm64.dmg
# drag Evermind.app to Applications, then right-click → Open on first launch
```

Set at least one API key on first launch (Settings → API keys). See [INSTALL.md](INSTALL.md) for a full walk-through.

## How agents work — node reference

Each agent has a single fixed role. Drag any subset onto the canvas and connect
them with arrows; downstream agents only run after all upstream agents pass.

| Node          | Role | Typical input | Typical output | Best paired with |
|---------------|------|---------------|----------------|------------------|
| **planner**   | Decompose the goal into 4-12 numbered subtasks; emit ownership boundaries (`builder_1` owns X, `builder_2` owns Y) | The user goal | JSON blueprint + per-agent briefs | always first |
| **analyst**   | Research the problem domain; gather 5-30 reference repos / docs; produce per-builder handoff sections | planner blueprint | XML-tagged dossier (~20-40 KB) with `<builder_N_handoff>`, `<reference_sites>`, `<deliverables_contract>` | game / website / dashboard |
| **imagegen**  | Plan visual assets — when no image backend is configured, produces a `manifest.json` + sprite atlas spec; when configured, generates real PNGs | analyst handoff | `assets/manifest.json` + `sprites.js` + `loader.js` | game / creative |
| **spritesheet** | Sprite atlas planner (offline / fast-path) | imagegen output | atlas coordinates spec | game (when 2D sprite-heavy) |
| **assetimport** | Asset pipeline coordinator — Wikimedia / Kenney prefetch | analyst preferred sites | local SVGs / PNGs in `assets/` | game / accessibility-critical sites |
| **builder×N** | Write the actual code (`/tmp/evermind_output/index.html` for primary; `module_b{N}.js` for peers) | analyst dossier | full HTML / JS / CSS | always — at least 1 |
| **merger**    | Integrate peer builders' modules into root index.html — uses HARD-SKIP fast-path when peers ship modules instead of duplicate index.html | builder outputs | merged single-page app | when builder count ≥ 2 |
| **reviewer**  | Run real Chromium against the live preview, take screenshots, audit interaction / responsive / completeness; emit accept / reject verdict | merged HTML | rejection list (or pass) | every shipped task |
| **patcher**   | Apply targeted SEARCH/REPLACE patches based on reviewer rejections — does NOT rebuild from scratch | reviewer verdict | patched HTML | reject path only |
| **tester**    | Headless browser smoke test — clicks the primary surface, watches console, fails if console errors > tolerance | deployed HTML | pass / fail + console log | every shipped task |
| **debugger**  | Surgical fixer for runtime errors tester caught | tester error log | debug-patch | reject + retry path |
| **deployer**  | List artifacts + emit preview URL (single-file → `/tmp/evermind_output/index.html`) | shipped HTML | preview URL + manifest | always last |

### Pre-built canvases

- **Simple** (`builder → tester → deployer`): smallest viable run, ~4-6 minutes, good for quick experiments
- **Standard** (`planner → analyst → builder → reviewer → patcher → tester → deployer`): default for most goals, ~12-18 minutes
- **Pro** (the full DAG above with imagegen / spritesheet / assetimport in parallel): asset-heavy games, polished landing pages, ~25-35 minutes
- **Ultra** (Pro × 4 + 24h budget + multi-file scaffolding): commercial-grade products

Pick a preset from the **Templates** button on the launchpad, or click **+ New Task** and arrange your own canvas.

### Building a custom DAG (5 steps)

1. **Click `+ New Task`** on the launchpad → Editor opens
2. **Drag agents** from the left sidebar onto the canvas
3. **Connect** by dragging from one agent's right edge to another's left edge — only forward edges allowed (no cycles, the planner can't depend on the deployer)
4. **Click an agent** to edit its task description (overrides the auto-generated one)
5. **Click `Run`** — every connected node runs in topological order; siblings without edges run in parallel automatically

**Common patterns:**

- *Three parallel builders:* connect `analyst → builder1`, `analyst → builder2`, `analyst → builder3`, then all three → `merger`. They start within 1s of each other and write to non-overlapping module files.
- *Quality-loop:* connect `reviewer → patcher → reviewer` (yes, a back-edge to itself is allowed once — capped at 1 rejection cycle to prevent infinite loops).
- *Speculative branch:* the runtime auto-spawns a "peer" speculatively for slow tool-using nodes — you don't have to wire this manually.

## Real-world usage

| Task type | Goal example | Recommended template | Typical duration |
|-----------|--------------|----------------------|------------------|
| 2D game (tower defense / arcade / puzzle) | "做一个2d植物大战僵尸，建模精致" | Pro | 25 min |
| 3D game (FPS / TPS / racing) | "Build a 3D first-person shooter with WASD + mouse look" | Pro / Ultra | 35 min |
| Single-page website | "Build a SaaS landing page for an AI startup" | Standard | 12 min |
| Multi-page website | "5-page ASL learning site with 26 letter cards" | Pro | 22 min |
| Data dashboard | "Sales analytics dashboard with 4 charts + filter sidebar" | Pro | 18 min |
| Tool / utility | "Unit converter, currency / length / weight, dark mode" | Simple | 5 min |
| Slides / presentation | "10-slide pitch deck for a fintech product" | Standard | 10 min |
| Creative / experimental | "Interactive generative-art canvas, mouse follow, particle field" | Standard | 12 min |
| Long task / commercial product | "End-to-end shop with cart, checkout, admin panel" | Ultra | 4-12 hours |

### Tips for great results

- **Be specific about the technology family.** "2d 塔防" routes to Phaser / Canvas2D; "3d shooter" routes to Three.js. Mixing both ("2d 但建模精致") is now correctly understood (since v7.4.2).
- **Mention the audience.** "for kids" → simpler colors, larger text. "commercial-grade" → polish + variety.
- **Pin specific page names.** For multi-page sites, list the exact filenames (`learn.html`, `progress.html`) and they will be honored.
- **Provide reference URLs.** Drop a GitHub link in the task description; analyst will fetch it as a reference.
- **For long tasks, switch to Ultra mode.** Standard / Pro have a 1-hour wall-time cap; Ultra extends to 24 hours and uses 4-builder parallelism.

## Troubleshooting

- **"Can't open Evermind because Apple cannot check it for malicious software"** → right-click the app → **Open** → confirm. macOS only asks once.
- **The launchpad asks for Files-and-Folders permission** → only on first launch under v7.3 or earlier. v7.4+ defaults workspace to `~/.evermind/workspace` (sandboxed).
- **Run is stuck at planner / analyst for 5+ minutes** → check Settings → API keys; relay endpoint may be unreachable. The trace viewer at the bottom of the editor shows the actual error.
- **Reviewer keeps rejecting the same issue** → click the agent on the canvas, expand its task description, paste the specific fix the reviewer wants. Patcher will apply it next iteration.
- **Need to cancel a stuck run** → click the red **stop** button in the toolbar; cancel releases the orchestrator immediately (since v7.4).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Electron main (Node)                                    │
│  ├── Next.js renderer (React + Monaco + Chromium view)   │
│  └── Spawns Python sidecar ──────────────┐               │
└──────────────────────────────────────────┼───────────────┘
                                           │
                        WebSocket / HTTP :8765
                                           │
┌──────────────────────────────────────────▼───────────────┐
│  Python FastAPI backend                                  │
│  ├── Pipeline engine (DAG executor, YAML harness)        │
│  ├── Agent router (planner/analyst/builder/reviewer)     │
│  ├── Quality guard (narration / empty-batch / kill)      │
│  ├── Provider plugins (Kimi / Qwen / DeepSeek / GLM /    │
│  │                     Doubao / MiniMax / OpenAI /       │
│  │                     Anthropic / Gemini / xAI /        │
│  │                     Mistral / Meta Llama)             │
│  └── Browser plugin (Playwright + AI cursor overlay)     │
└──────────────────────────────────────────────────────────┘
```

## Screenshots

| Pipeline canvas | Chat + editor |
|-----------------|---------------|
| ![pipeline](docs/assets/ss-pipeline.png) | ![chat](docs/assets/ss-chat.png) |

| Agent browser | Live trace |
|---------------|------------|
| ![browser](docs/assets/ss-browser.png) | ![trace](docs/assets/ss-trace.png) |

*(Add the screenshots under `docs/assets/` before tagging a release.)*

## Install

- **Users**: [INSTALL.md](INSTALL.md) — download .dmg and run
- **Developers**: [BUILD.md](BUILD.md) — build from source + Apple signing guide

## Contributing

PRs welcome. Before opening one:

```bash
python3 -m pytest backend/tests -q        # backend
cd frontend && npx tsc --noEmit           # types
```

Add an entry to `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md). Sign your commit with `git commit -s`. Open with a description of the intent and a short test plan.

## Safety

Every agent action that writes files, executes shell, or navigates the browser is logged in the live trace. Support-lane builders cannot write to the root `index.html`. The browser plugin defaults to headless; overlays are on so the user always sees what's happening. See [docs/safety.md](docs/safety.md) for the full model.

## License

[Apache 2.0](LICENSE). Commercial, modification, distribution all allowed — the license only asks you to state the changes you made, keep the license text, and not use the "Evermind" trademark as your own product name.

## Credits

Evermind learns from the best in class:

- [Aider](https://github.com/Aider-AI/aider) — editblock / whole / udiff output formats
- [Cline](https://github.com/cline/cline) — BYOK + safety model
- [OpenHands](https://github.com/All-Hands-AI/OpenHands) — agent runtime concepts
- [bolt.diy](https://github.com/stackblitz-labs/bolt.diy) — multi-provider UX
- [browser-use](https://github.com/browser-use/browser-use) — AX-tree snapshot + element highlight
- [rrweb](https://github.com/rrweb-io/rrweb) — mouse replay cursor animation
- [ghost-cursor](https://github.com/Xetera/ghost-cursor) — Bezier mouse movement
- [Monaco Editor](https://github.com/microsoft/monaco-editor)
- [FastAPI](https://github.com/tiangolo/fastapi)

## Project philosophy

Evermind is built on three beliefs:

1. **LLM vendors are not interchangeable.** Kimi, Qwen, DeepSeek, Doubao, MiniMax, OpenAI, Anthropic, Gemini each have different reasoning fields, tool schemas, retry rules. The right abstraction is one plugin per vendor — not one OpenAI-compat middleman for all of them.

2. **Weak models can be made strong with better plumbing.** The difference between a model that works and one that doesn't is often a single `enable_thinking=False` or a `<think>` tag that nobody stripped. Evermind's quality guard closes those gaps.

3. **Agents must be visible.** When the AI drives your browser, you should see a cursor, a ripple, a scroll glow, a highlighted target — not a frozen screen with a status string.
