SOURCE-FIRST RESEARCH LOOP

Use this skill when the node needs research, planning, or inspiration gathering.

Core workflow:
- Start with primary implementation sources: GitHub repos, official docs, framework examples, technical tutorials, devlogs, postmortems.
- Treat polished marketing/product sites as supporting evidence only, not the main source of implementation truth.
- Prefer references that expose structure: source trees, README architecture notes, code snippets, issue threads, design breakdowns.
- Extract patterns that downstream nodes can execute directly: page map, animation system, navigation model, asset strategy, quality risks.

Required output shape:
- 3-6 trusted source links with one-line justification each.
- Concrete implementation patterns worth copying or adapting.
- Risks and anti-patterns to avoid.
- A downstream brief for builder/polisher/reviewer with clear acceptance criteria.

Task-specific OSS seed families:
- Browser 3D / TPS / shooter: `pmndrs/ecctrl`, `gdquest-demos/godot-4-3d-third-person-controller`, `donmccurdy/three-pathfinding`, `Mugen87/yuka`, `KhronosGroup/glTF-Sample-Assets`.
- PPT / deck / presentation software: `revealjs/reveal.js`, `gitbrent/PptxGenJS`, `marp-team/marp-core`.
- Bug review / browser QA / regression: `microsoft/playwright`, `reg-viz/reg-suit`, `BackstopJS/BackstopJS`.
- Multi-agent orchestration / role split / repair loops: `microsoft/autogen`, `All-Hands-AI/OpenHands`, `FoundationAgents/MetaGPT`, `crewAIInc/crewAI`, `FoundationAgents/OpenManus`.

Routing rule:
- Match sources to the node's actual job. Analyst should pass controller/AI/runtime sources to builders, export/layout sources to PPT builders, and QA/regression sources to reviewer/tester instead of dumping the same list on everyone.

Do not:
- Waste time browsing visually impressive but opaque landing pages with no inspectable implementation clues.
- Produce vague inspiration boards with no actionable engineering guidance.
