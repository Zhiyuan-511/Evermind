# Evermind

> A multi-agent collaboration pipeline that turns a single sentence into a complete, deliverable website or web game.
> Local-first · Desktop · 8–12 node DAG · Pro mode runs 30–50 minutes and emits a production-ready HTML/CSS/JS project.

[中文版 README →](./README.zh.md)

![Status](https://img.shields.io/badge/status-alpha-orange)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Table of contents

1. [About the author](#about-the-author-please-read)
2. [Project philosophy](#project-philosophy)
3. [What the project does](#what-the-project-does)
4. [Why a DAG of agents (and not one big agent)](#why-a-dag-of-agents-and-not-one-big-agent)
5. [Pipeline orchestration in depth](#pipeline-orchestration-in-depth)
6. [The reviewer ↔ patcher loop](#the-reviewer--patcher-loop-the-core-quality-guarantee)
7. [Agent role catalogue](#agent-role-catalogue)
8. [Architecture highlights](#architecture-highlights)
9. [Platforms](#platforms)
10. [How to use it](#how-to-use-it)
11. [Known limitations](#known-limitations-full-disclosure-before-open-source)
12. [Roadmap](#things-i-plan-to-keep-optimising)
13. [Feedback / contributing](#feedback-channels)

---

## About the author (please read)

I'm a **17-year-old high school student** and **I don't really know how to code**, but I'm fascinated by AI.

Evermind is a project I iterated into existence by repeatedly trial-and-erroring with various AI tools. The core ideas, the architecture decisions, every bug-fix round (from v6.x all the way to v7.62) — I worked through each of them, formed my own plan, then drove it to landing. I made an honest effort to understand what every line of the code is doing.

**Honestly**: there are still a lot of things in this project that are beyond my current ability to optimize. For example:
- Deeper optimization of multi-model routing and token cache scheduling
- Stability of the browser automation interaction layer
- Further quality lifts in Three.js / WebGL content generation
- Tracking down complex Python asyncio deadlocks

I'll **keep learning** and patch these gaps over time. If you're an experienced developer and you spot something written poorly or have a better approach, I really welcome an Issue or a direct PR — every suggestion you give me is a free "teacher".

**Contact**: open a GitHub Issue with feedback, or chat in Discussions.

---

## Project philosophy

I started this project after I noticed something: when I asked a single LLM (any of them — GPT, Claude, Kimi, etc.) to "build a complete website with 3D background and three pages", the answer was always plausible-looking but rarely *runnable end-to-end*. The model would write a beautiful header, then run out of patience and cut the JavaScript short with `// TODO: implement scroll animation here`. Or it would invent a `cdn.fakelib.com` link that doesn't exist. Or it would write Three.js code without actually loading the Three.js library.

What changed everything was watching what happens when **multiple specialized agents collaborate under a strict supervisor** — like how a real software team works. There's a planner who decomposes the goal. An analyst who researches reference implementations. A designer who fixes the visual language. Two builders who each write code in parallel. A merger who reconciles their outputs. A reviewer who literally opens the result in a browser, clicks the buttons, and reports back what's broken. A patcher who applies surgical fixes based on the reviewer's specific complaints.

That's Evermind in one paragraph.

The **five core beliefs** that drive every design decision:

### 1. Specialization beats generalism

A single agent trying to do "research + design + code + test" all at once produces mediocre output everywhere. Six specialists, each doing one thing under a tight prompt contract, produce work that's coherent at every layer. Each role has its own YAML prompt template (`backend/prompt_templates/*.yaml`) that's been ground down to ~3K characters of dense, opinionated guidance.

### 2. Real-browser verification is non-negotiable

Static heuristics (does the HTML parse, does the JS lint clean, does the file have N lines) catch ~30% of failure modes. The other 70% — broken Three.js render loop, dangling `addEventListener` with no handler, button click does nothing, image src 404s — only surface when you actually open the page in Chromium and interact with it. Evermind's reviewer node runs Playwright against the artifact, takes a screenshot, clicks the primary CTA, scrolls, and emits a structured JSON verdict.

### 3. Patches beat rewrites

When the reviewer rejects, the temptation is to ask the LLM to "rewrite the whole file". This wastes 40K tokens, breaks all the things that *did* work, and rarely converges. Instead, the patcher receives the reviewer's exact `blocking_issues` list (each with `file`, `anchor_line_range`, `current_excerpt`, `suggested_fix`) and emits surgical SEARCH/REPLACE blocks. Net effect: 10× fewer tokens, no regressions, fast convergence.

### 4. Strict obedience to user intent

If you say "Awwwards-level 3D site with cyan + magenta neon palette", the planner's blueprint must say exactly that, and **every downstream node must inherit that blueprint** (v7.57 PLANNER BLUEPRINT injection). The most common failure mode in early Evermind versions was: planner promises 3D, builder writes a 2D landing page anyway. Fix: the orchestrator now actively re-injects the planner's blueprint into the system prompts of builder, merger, polisher, patcher, and debugger — so no node "forgets" the contract.

### 5. Local-first, vendor-neutral

You run the desktop app on your laptop. Your code never leaves your disk. Your API key only flows to the LLM endpoint *you* configure (OpenAI, Anthropic, Kimi, DeepSeek, your own relay — anything OpenAI-compatible). No telemetry. No "Evermind cloud". No analytics endpoints. The only network calls are: (a) your LLM provider, (b) optional online research by the analyst node (browser automation, fully under your control), (c) downloading reference assets from the URLs the analyst returns. The decision: **if you can't audit the network behaviour with `nettop`, you shouldn't be running an "AI agent" on your machine**.

---

## What the project does

You give it one sentence or a longer prompt, e.g.:

> "Build a futuristic-tech 3D website with a WebGL background, scroll-driven animation, dark neon palette, three pages (home + features + pricing), and Awwwards-level micro-interactions."

Evermind's 12 agent roles (8–12 of them activate depending on task type) **collaborate** to deliver:

1. **Planner** breaks the requirement down into a precise execution blueprint — which pages, what each page needs, what's mandatory vs optional
2. **Analyst** does online research on reference implementations: Three.js patterns, tech stacks of similar Awwwards sites
3. **UI Design** produces design tokens (palette / type scale / spacing) plus a layout blueprint
4. **Scribe** writes copy and content architecture
5. **Builder 1+2 in parallel** write HTML/CSS/JS code (per the blueprint — home page + sub-pages)
6. **Merger** unifies the two builder outputs into a single coherent project
7. **Polisher** polishes motion, whitespace, and transitions (no structural changes)
8. **Reviewer** opens the artifact in a real Playwright browser, audits quality, and scores 0–10
9. **Patcher** applies precise SEARCH/REPLACE fixes when the reviewer rejects, driven by the reviewer's specific feedback
10. **Deployer** returns a local preview URL
11. **Debugger** fixes runtime errors
12. **Tester** runs interaction tests (click / drag / keyboard)

The final output is a **real, browser-runnable** complete website project containing `index.html`, `styles.css`, `app.js`, and possibly multiple pages like `about.html` / `pricing.html`.

---

## Why a DAG of agents (and not one big agent)

A common alternative is "one big agent with tools" (Aider, Cursor agents, ChatGPT Code Interpreter). They're great for *editing* an existing codebase but underperform at **building from scratch under a complex brief** for two reasons:

1. **Context window saturation**: a single agent that researches references, designs the palette, writes 4 files, reviews itself, and patches mistakes runs through the model's attention budget by the time it's halfway through the build. Quality of late decisions degrades visibly.
2. **No independent QA**: a single agent grading its own work has a strong "looks-fine-to-me" bias. Even Claude / GPT-5 admit "yes, this looks done" when half the click handlers are stubs. An *independent* reviewer node, with a fresh context, will catch what the builder rationalized away.

Evermind's DAG explicitly:
- Hands each role a **fresh, narrow context** (only the brief + handoff from upstream — never the full conversation).
- Runs builders **in parallel** to get diverse implementations, then has a merger pick the best parts.
- Forces the reviewer to use **a separate, faster, non-thinking model** (`kimi-k2.5`) precisely because thinking models forget to emit the JSON verdict (v7.62).
- Caps the reviewer↔patcher loop at **3 rounds maximum** — past that, you're chasing perfection in a way the LLM can't deliver, so we ship the best snapshot.

---

## Pipeline orchestration in depth

### Pipeline structure (pro mode)

```
                                    ┌─→ imagegen ─┐
planner → analyst ──────────────────┼─→ spritesheet ──┐    (asset_heavy tasks only)
                                    └─→ assetimport ──┘
                  │                                    │
                  └─→ uidesign + scribe ──┐            │
                                          ↓            ↓
                              ┌── builder 1 ──→ merger
                              └── builder 2 ──→
                                                │
                                                ↓
                                          polisher (optional)
                                                │
                                                ↓
                                          reviewer ←──┐
                                                │     │
                                          ┌─────┤     │ (reject — up to 3 rounds)
                                          │     ↓     │
                                       (pass)  patcher ┘
                                          │
                                          ↓
                                       deployer → debugger → tester
```

### Phase-by-phase walkthrough

| Phase | Nodes | Purpose | Typical duration |
|---|---|---|---|
| **1. Plan** | planner | Decompose user goal into a structured blueprint with explicit page list, mandatory features, and "must avoid" rules | 30–90s |
| **2. Research** | analyst | Hit GitHub / docs sites / reference implementations; produce ~2–10K char technical brief; download up to 8 reference URLs | 1–4 min |
| **3. Asset prep** *(asset_heavy only)* | imagegen + spritesheet + assetimport | Generate game sprites, pack into atlas, import reference images | 2–6 min |
| **4. Design + copy** | uidesign + scribe | Output design tokens (palette, type scale, spacing, motion) and finalized site copy / narrative | 30–90s each (parallel) |
| **5. Build** | builder1 + builder2 | Two builders write code in parallel — one focuses on home/main, the other on subpages or a different aspect (e.g. the game logic vs the menu) | 3–8 min each (parallel) |
| **6. Merge** | merger | Reconcile the two builders' outputs; pick the better implementation for each shared file; resolve conflicts | 1–4 min |
| **7. Polish** *(optional)* | polisher | Tweak motion timing, whitespace, transitions, micro-interactions — never restructures | 1–3 min |
| **8. Audit** | reviewer | Open in Playwright, take screenshots, click CTAs, scroll, scrape DOM, emit JSON verdict with `score`, `verdict`, `blocking_issues[]` | 1–3 min |
| **9. Repair** *(only if rejected)* | patcher | Apply SEARCH/REPLACE blocks per reviewer's blocking_issues; re-trigger reviewer audit | 30s–2 min per round |
| **10. Deploy** | deployer | Write the artifact to `~/.evermind/workspaces/<task>/` and emit `http://127.0.0.1:8765/preview/<task>/` | <5s |
| **11. Debug** | debugger | Inspect browser console errors / runtime exceptions; suggest fixes | 30s–2 min |
| **12. Test** | tester | Programmatic interaction tests (click each button, fill each form, drag each draggable) | 1–3 min |

Total Pro-mode wall-clock time: **30–50 minutes** for a typical multi-page website. **Standard mode** (no patcher loop, no asset pipeline) is **8–15 minutes**. **Simple mode** (4 nodes) is **3–5 minutes**.

### Mode comparison

| Mode | Active nodes | Reviewer loop | Asset pipeline | Best for |
|---|---|---|---|---|
| **Simple** | planner → builder → reviewer → deployer | No | No | Single page, one feature, fast iteration |
| **Standard** | + analyst + uidesign + polisher | No | No | Multi-section landing page, moderate visuals |
| **Pro** | + builder2 + merger + patcher | **Yes (≤3 rounds)** | Auto-detected (game / asset_heavy) | 3D sites, multi-page, web games, dashboards |
| **Ultra** | All + 4 builders + 24h timeout | Yes (≤5 rounds) | Yes | Full mini-app projects, multi-day scope |

### Topology reset (the secret sauce)

When the reviewer rejects, downstream nodes (deployer / debugger / tester) **don't just stay in their previous state** — the orchestrator resets them to PENDING and re-runs. This means: after a patcher fix, the deployer will publish the patched version, the tester will re-test the patched buttons, and the debugger gets a fresh shot at console errors. **The patched version is always what ships, not the pre-patch version.** This is enforced in `orchestrator.py:_topology_reset_on_reviewer_reject`.

### Capability-aware build

Before any builder runs, the **task classifier** (`backend/task_classifier.py`) inspects the user prompt against 10 capability patterns:

| Capability | Trigger keywords | Auto-injected contract |
|---|---|---|
| `webgl_3d` | three.js / WebGL / 3D / immersive / shader | Must `<script src="three.min.js">` + scene + camera + animate() |
| `canvas_2d_game` | game / arcade / sprite / collision | Must use `<canvas>` + `requestAnimationFrame` + game loop |
| `canvas_art` | generative / particle / fractal / visualization | Must use `<canvas>` + draw loop + interaction handlers |
| `shader_glsl` | shader / GLSL / fragment | Must include working vertex + fragment shader |
| `audio_reactive` | audio / music visualizer / waveform | Must use Web Audio API + analyser node |
| `physics_engine` | physics / matter.js / cannon | Must include physics simulation |
| `drag_drop` | drag / drop / kanban / sortable / 拖拽 | Must use HTML5 Drag-and-Drop API or pointer events |
| `real_time_chart` | dashboard / live data / chart | Must use Chart.js / D3 / SVG + animation frame |
| `video_embed` | video / cinematic / reel | Must use `<video>` with proper poster + autoplay handling |
| `scroll_driven` | scroll / parallax / scrollytelling | Must use IntersectionObserver or scroll-driven animations |

Each match contributes a hard contract to the builder's system prompt, like:
> "REQUIRED CAPABILITY: webgl_3d. You MUST `<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js">` AND write a `scene = new THREE.Scene()` setup AND call `renderer.render(scene, camera)` inside a `requestAnimationFrame` loop. A static 2D landing page does NOT count."

This is why a "build me a futuristic 3D site" prompt actually produces working Three.js code instead of a CSS gradient that looks 3D-ish.

---

## The reviewer ↔ patcher loop (the core quality guarantee)

This is the key design that separates Evermind from "single-agent one-shot generation":

1. **Reviewer** opens the builder output in a real browser (Playwright Chromium), runs interaction tests, and emits a structured JSON verdict with this shape:
   ```json
   {
     "score": 7.25,
     "verdict": "approve" | "reject",
     "blocking_issues": [
       {
         "id": "issue-1",
         "category": "interaction" | "visual" | "content" | "performance",
         "severity": "blocker" | "major" | "minor",
         "file": "app.js",
         "anchor_line_range": [42, 58],
         "current_excerpt": "document.addEventListener('keydown'",
         "suggested_fix": "Move the WASD listener inside DOMContentLoaded, and apply move vector to camera.position.",
         "description": "Pressing W/A/S/D does nothing — the listener is registered but never updates camera.",
         "severity_rationale": "This is a blocker because the user cannot navigate the 3D scene."
       }
     ],
     "praise": ["Hero gradient is striking", "Typography hierarchy is clean"]
   }
   ```
2. If rejected, **Patcher** takes those precise instructions and applies block-level SEARCH/REPLACE fixes. The patcher's prompt is dramatically simpler than the builder's: just "given these N blocking issues with this exact code excerpt, emit SEARCH/REPLACE blocks". No rewrites. No new features.
3. After patching, **Reviewer audits again** — with a fresh context that includes a `[ROUND-N]` signal so it knows this is a re-audit (v7.61).
4. The loop runs at most **3 rounds** (configurable in Settings → Reviewer Reject Budget). Past 3 rounds we ship the highest-scoring snapshot the patcher produced (v7.35 regression-detection rolls back to a better earlier version if patches made things worse).

**Why not let one agent write it perfectly in one shot?**
A single LLM writing a complex site one-shot averages "barely-runs and stops" quality. Multi-agent collaboration plus strict gating lets each role focus on what it's best at, and the final browser verification catches what no single-agent setup can.

**Why is the reviewer a fast non-thinking model?**
Counterintuitively, top thinking models (kimi-k2.6-code-preview / o1 / opus) are *worse* reviewers — they produce 5K characters of reasoning prose and forget to emit the JSON verdict, so the patcher has nothing actionable. The fast model (kimi-k2.5) emits the JSON immediately. This was the v7.62 fix.

---

## Agent role catalogue

| Agent | Lines of prompt | Default model | Output format | Typical input → output |
|---|---|---|---|---|
| **planner** | ~250 | kimi-coding | Structured blueprint (markdown) | "build a 3D site" → blueprint with page list + mandatory features + risks |
| **analyst** | ~300 | kimi-k2.6-code-preview | Technical brief + reference URLs | Blueprint → 8K char brief with Three.js patterns, similar Awwwards sites |
| **uidesign** | ~100 | kimi-k2.5 | Design tokens (palette, type, space) | Brief → full design system + layout blueprint |
| **scribe** | ~80 | kimi-k2.5 | Copy + content architecture | Brief → page-by-page content draft |
| **imagegen** | ~120 | (image API, e.g. DALL-E 3) | Image files | Brief → 4–10 generated images saved to workspace |
| **spritesheet** | ~80 | kimi-coding | Atlas image + JSON map | Image set → packed sprite atlas |
| **assetimport** | ~60 | kimi-coding | Local files + manifest | URLs → downloaded local assets with attribution |
| **builder1 / builder2** | ~600 each | kimi-coding | HTML/CSS/JS files (multifile) | Brief + tokens + copy → working code |
| **merger** | ~200 | kimi-coding | Unified file set (SEARCH/REPLACE) | 2 file trees → 1 coherent file tree |
| **polisher** | ~150 | kimi-k2.5 | Motion/spacing tweaks (SEARCH/REPLACE) | Code → polished code |
| **reviewer** | ~500 | **kimi-k2.5** *(forced fast-path v7.62)* | JSON verdict | Code (browser-rendered) → score + blocking_issues |
| **patcher** | ~150 | kimi-coding | SEARCH/REPLACE blocks | Verdict → surgical fixes |
| **deployer** | ~50 | (no LLM — deterministic) | Preview URL | File tree → `http://127.0.0.1:8765/preview/...` |
| **debugger** | ~100 | kimi-k2.5 | Console error explanation + fix | Browser console errors → suggested patches |
| **tester** | ~120 | kimi-k2.5 | Pass/fail per interaction | Code + interaction list → result matrix |

All prompts are **YAML files in `backend/prompt_templates/`** — edit them, save, and the next run picks up the change (no rebuild needed).

---

## Architecture highlights

### 12 agents + 1 dynamic scheduler

Each agent's prompt template lives in `backend/prompt_templates/*.yaml`, edited and hot-reloaded independently.

The scheduler (`backend/orchestrator.py`, ~25K lines) handles:
- DAG topological sort + topology reset (when reviewer triggers a redo, all downstream nodes return to PENDING)
- Node timeouts + retries + model fallback chains
- Context propagation (v7.57 actively injects the planner blueprint into every downstream code-producing node)
- Browser automation (Playwright) for the reviewer

### Capability-aware builder (v7.56)

Recognises 10 capability keywords (WebGL/Three.js / 2D games / canvas art / GLSL shaders / Web Audio / physics / drag-drop / real-time charts / video embed / scroll-driven), and auto-injects matching "must implement" contracts into the builder system prompt. When the LLM sees "3D immersive" in the task, it loads Three.js inline and writes a real `scene` / `camera` / `animate()` setup instead of a stub.

### Auto post-process (v7.56d / v7.58)

- Auto-detects HTML containing `<canvas>` but missing a Three.js library tag, then injects the `<script src=>` automatically
- Auto-rewrites `#canvas { z-index: 0 }` to `z-index: -1` (so it doesn't occlude hero content)
- Adds a null-guard around Three.js render calls automatically

### Reviewer fast-path (v7.62, the last critical fix)

The reviewer uses a fast, non-thinking model (`kimi-k2.5`) so it always emits a JSON verdict. Thinking models tend to produce a lot of reasoning text but forget the JSON, which strands the patcher with no actionable brief and triggers a death-loop. This was the last core fix before open-sourcing.

### Local-first networking model

```
Your laptop                                           Internet
─────────────                                        ──────────
[Evermind.app]
  ├─ Electron front-end (renderer:3000)              ✗ no outbound
  ├─ Python backend (FastAPI on 127.0.0.1:8765)     ─→ LLM API (your configured endpoint)
  │     ├─ Orchestrator                              ─→ analyst HTTP fetch (only URLs returned by LLM)
  │     ├─ Reviewer (Playwright Chromium)            ─→ render of local file:// preview
  │     └─ Workspaces (~/.evermind/workspaces/)
  └─ MCP server (stdio for Claude Code/Cursor)
```

There is **no Evermind-operated server**. There is **no telemetry endpoint**. The backend binds to `127.0.0.1` only — no LAN access. The MCP server uses stdio (no network). If you `nettop` the process, the only outbound connections you should see are: (a) the LLM endpoint you configured in Settings, (b) reference URLs the analyst node decided to fetch (you can see the URL list in each run's analyst report).

---

## Platforms

> **Honest status as of this release** — please read before installing:

| Platform | Build available? | Tested? | Status |
|---|---|---|---|
| **macOS** (Apple Silicon) | ✅ DMG | ✅ Daily | **Supported** |
| **macOS** (Intel) | ✅ DMG | 🟡 Occasionally | **Supported** (untested often, but no known issues) |
| **Windows 10/11** | ❌ No `.exe` artifact | ❌ Never built | **Not supported yet** — `electron-builder` config has no `win` target, and several internals (`lsof`, `codesign`, `xattr`, `/Library/Frameworks` paths) are macOS-coded. A Windows port is planned but not done. |
| **Linux** | ❌ No `.AppImage` | ❌ Never built | **Not supported yet** — same reason as Windows. |

**Workaround for Windows / Linux users today**: you can run the **backend** standalone (`python3 backend/server.py`) and the **frontend dev server** (`cd frontend && npm run dev`) separately — you'll lose the desktop integration but the web editor works at `http://127.0.0.1:3000/editor`. PRs to add a real Windows port are extremely welcome — see [issue tracker](https://github.com/Zhiyuan-511/Evermind/issues) for the open "Windows port" task.

If you're a Windows / Linux developer and want to help port:
1. Open the [Windows port tracking issue](https://github.com/Zhiyuan-511/Evermind/issues) to coordinate
2. The known blockers are listed there: PATH delimiter (`:` → `path.delimiter`), `lsof` → `netstat`, Python finder paths, Electron `win` target, GitHub Actions `windows-latest` runner
3. PRs welcome — even partial fixes (e.g. just the PATH delimiter) are worth merging

---

## How to use it

### 1. Install

**macOS (recommended path)**: Download the latest DMG from [Releases](https://github.com/Zhiyuan-511/Evermind/releases) and drag it into Applications.

**Windows**: There's no pre-built `.exe` yet. Build from source — see [BUILD.md](./BUILD.md). PRs to publish a Windows artifact are very welcome.

**Linux**: Same — build from source. Some users have reported success with the `.AppImage` target.

On macOS first launch, the app will ask permission to access the Desktop folder (used to store generated sites). That's the only OS permission required for normal use.

### 2. Configure your API key

Open Evermind → Settings → enter your LLM API key:
- **Recommended**: Kimi (Moonshot) — `sk-...` — fastest in CN, friendly to Chinese prompts
- Also supported: OpenAI / Anthropic / DeepSeek / Qwen / Doubao / Zhipu / MiniMax
- If you're using a third-party relay, just fill in the corresponding base URL.

API keys are stored Fernet-encrypted at `~/.evermind/config.json` with the symmetric key in `~/.evermind/.key` (filesystem-protected). Back up `.key` if you want to keep the same encrypted store across rebuilds.

### 3. Write a prompt and run

**Simple mode** (4 nodes, 3–5 min):
For simple pages, e.g. "a counter web page" or "a three-page coffee shop site".

**Standard mode** (6–8 nodes, 8–15 min):
Mid-complexity work, includes reviewer but no patcher loop.

**Pro mode** (11 nodes, 30–50 min):
Full pipeline including reviewer↔patcher loop and the asset pipeline.
Best for: 3D WebGL sites, multi-page commercial sites, web games, complex dashboards.

**Ultra mode**: experimental, long-running tasks (24h ceiling).

### 4. The more detailed your prompt, the better the output

Evermind's planner and builders **strictly follow your prompt**. A useful template:

```
Role: you are a senior WebGL creative developer with 10 years of experience
Task: futuristic-tech 3D website
Requirements:
1. Three.js-powered hero-area 3D WebGL background
2. Scroll-driven section entrance animations (IntersectionObserver / GSAP ScrollTrigger)
3. At least three pages: home / features / contact
4. Palette: dark neon (charcoal + cyan #00f0ff + magenta #ff00aa)
5. Awwwards-level smooth transitions plus micro-interactions
```

The more specific the prompt, the more obedient the nodes — that is precisely the design intent of v7.57's PLANNER BLUEPRINT injection mechanism.

### 5. Custom canvas

You can also start from a blank canvas and build your own DAG by dragging nodes (n8n-style). See [docs/NODE_GUIDE.md](./docs/NODE_GUIDE.md).

---

## Known limitations (full disclosure before open-source)

- **Platform**: only tested on macOS (arm64 + x64). Windows/Linux are untested; in theory the Python backend should run, but the Electron frontend would need to be repackaged.
- **3D site quality**: after v7.62 the builder really does write Three.js code, but the end result is around "Awwwards-mid" level. It's not yet top award-winning (that needs the lygia GLSL library and GSAP ScrollTrigger to be merged in — planned for the next release after open-sourcing).
- **Polisher occasional timeout**: when the deterministic gap gate fails, polisher fails — but the pipeline skips and continues, so it doesn't block final delivery.
- **JSON output stability across LLMs**: despite multiple safety nets (v7.56b ESCAPE HATCH + v7.59 top-of-prompt forced JSON directive + forced-synthesis fallback), some LLMs still occasionally fail to emit JSON.
- **Token cost**: a Pro-mode run consumes roughly 200K–500K tokens (including the reviewer's multi-round browser QA). It's a good idea to iterate the prompt in Simple mode first, then run Pro.

---

## Things I plan to keep optimising

1. **Multi-model ensemble** — let builder1 and builder2 use different models (GPT / Kimi / Claude), and let the merger pick the better output
2. **3D asset library fusion** — make lygia GLSL, GSAP ScrollTrigger, and Lenis smooth scroll first-class building blocks
3. **Localised LLM routing** — direct connections to Kimi / Doubao / Qwen for users in mainland China, no relay needed
4. **A finer reviewer rubric** — expand the current 6-dimension scoring to 12 dimensions
5. **Sturdier Three.js / Canvas post-processing** — Bloom / FilmGrain / EffectComposer enabled by default
6. **Windows / Linux first-class support** — published `.exe` and `.AppImage` per release
7. **Full UI internationalisation** — extract all UI strings to a translation file so non-Chinese users get a fully English experience by default

---

## Feedback channels

- **GitHub Issues**: bug reports, suggestions, architecture discussions
- **GitHub Discussions**: usage notes, prompt-template contributions
- **Pull Requests**: very welcome — especially for prompt-engineering improvements, new capability-recognition regexes, and post-processing fixes

I'm a beginner. I'll **learn how to review PRs slowly** — possibly slower than experienced maintainers — but I'll read every line you propose carefully and come ask you questions.

---

## Acknowledgements

- All the LLM providers who make this possible (users switch freely in Settings)
- Open-source projects like Three.js / GSAP / Playwright
- Everyone who files an Issue or sends a PR — you make sure a high-school student isn't doing this alone

---

## License

MIT — free to use, modify, and redistribute.

---

> **If you find this project interesting, please star it so I know.**
> **If you're willing to point out anything I got wrong, I'd be enormously grateful.**
