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
curl -LO https://github.com/Zhiyuan-511/Evermind/releases/latest/download/Evermind-arm64.dmg
open Evermind-arm64.dmg
# drag Evermind.app to Applications, then right-click → Open on first launch
```

Set at least one API key on first launch (Settings → API keys). See [INSTALL.md](INSTALL.md) for a full walk-through.

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
