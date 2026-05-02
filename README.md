# Evermind

> A multi-agent collaboration pipeline that turns a single sentence into a complete, deliverable website or web game.
> Local-first · Desktop · 8–12 node DAG · Pro mode runs 30–50 minutes and emits a production-ready HTML/CSS/JS project.

[中文版 README →](./README.zh.md)

![Status](https://img.shields.io/badge/status-alpha-orange)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## About the Author (please read)

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

## What this project does

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

## Pipeline orchestration

### Pipeline structure (pro mode)

```
                                    ┌─→ imagegen ─┐
planner → analyst ──────────────────┼─→ spritesheet ──┐    (asset_heavy tasks)
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
                                          ┌─────┤     │ (reject)
                                          │     ↓     │
                                       (pass)  patcher ┘ (up to 3 rounds)
                                          │
                                          ↓
                                       deployer → debugger → tester
```

### Reviewer ↔ Patcher loop (the core quality guarantee)

This is the key design that separates Evermind from "single-agent one-shot generation":

1. **Reviewer** opens the builder output in a real browser, runs interaction tests, and emits a structured JSON verdict (with a `blocking_issues` array — each entry carries `file` / `anchor_line_range` / `suggested_fix`)
2. If rejected, **Patcher** takes those precise instructions and applies block-level SEARCH/REPLACE fixes
3. After patching, **Reviewer audits again** (it does NOT just accept the patcher's output)
4. The loop runs at most 3 rounds — this enforces output quality

**Why not let one agent write it perfectly in one shot?**
A single LLM writing a complex site one-shot averages "barely-runs and stops" quality. Multi-agent collaboration plus strict gating lets each role focus on what it's best at, and the final browser verification catches what no single-agent setup can.

---

## How to use it

### 1. Install (macOS desktop)

Download the latest DMG from [Releases](https://github.com/Zhiyuan-511/Evermind/releases) and drag it into Applications.

On first launch it will request permission to access the Desktop folder (used to store generated sites).

### 2. Configure your API key

Open Evermind → Settings → enter your LLM API key:
- **Recommended**: Kimi (Moonshot) — `sk-...` — fastest in CN, friendly to Chinese prompts
- Also supported: OpenAI / Anthropic / DeepSeek / Qwen / etc.
- If you're using a third-party relay, just fill in the corresponding base URL.

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
