# Changelog

All notable changes to Evermind are documented in this file.

This project follows [Semantic Versioning](https://semver.org/) loosely; major releases (V*) bundle architectural changes, minor releases ship cumulative fixes.

## [v7.7.0] — 2026-04-27 — First open-source release

This is the inaugural public release. The release combines hardening from v7.5/v7.6 internal rounds with last-mile fixes for cross-task pollution, asset pipeline routing, and chat agent stability.

### Security
- **Removed hardcoded private LLM relay endpoint (`api.relay.com`)** from `MODEL_REGISTRY`. Default `api_base` for the GPT family is now `api.openai.com/v1`. Users who want a relay (relay / relay / etc.) configure it explicitly in **Settings → API Keys** per provider.
- `.env.example` now defaults `HOST=127.0.0.1` (was `0.0.0.0`). The unencrypted backend port stays bound to localhost unless explicitly opted out.

### Added
- `LICENSE` (MIT)
- `INSTALL.md` (end-user installation, permission popups, troubleshooting)
- `BUILD.md` (build-from-source steps, dev stack, env vars)
- `CHANGELOG.md` (this file)
- Settings → Node Models tab now exposes **Reviewer Reject Budget**. v7.7 ships with single-loop closure (1) as the only effective option; multi-round budgets (2/3/5) are placeholders pending v7.8 scheduler work.
- Code-stats badge on planner/analyst canvas nodes — falls back to `len(full_output)` when no code files were written, so non-builder roles surface real produce volume instead of `0 lines`.

### Fixed
- **Cross-task chat session pollution**: clicking *New Task* on the Launchpad now navigates to `/editor?fresh=1` and the editor unconditionally spawns a brand-new chat session, clears the canvas, and unsets the active task. Backend `session_continuation` no longer auto-injects the previous task's plan into a new run.
- **PvZ-style asset pipeline routing**: `_PRO_ASSET_HEAVY_RE` and `_GAME_EXPLICIT_ASSET_PIPELINE_RE` now match `建模精细 / 精细建模 / 建模精美 / 精致美术` plus PvZ-class signals (`植物大战僵尸`, `tower defense`, `multiple distinct enemies`). English equivalents (`fine modeling`, `polished sprites`, `pixel art`, `tower defense`, `detailed character art`, etc.) added so English briefs land on the same asset pipeline.
- **Chat agent kimi 400 tool_call_id**: chat handler now reuses the synthesised `_tc_id` between the tool-call announcement frame and the follow-up `role:tool` message. Eliminates `400 tool_call_id is not found` on relays that occasionally return tool_call objects with empty `id`.
- **Pipeline header `0/12` even with 8 NEs passed**: removed the `selectedRun?.id === activeRun.id` gate from `summaryCompletedNodes`. Counter now reflects actual store state across re-renders.
- **`max_retries` user setting now drives reviewer reject default**: setting "Max Retries: 3" makes the reviewer budget default to 3 rounds (still capped to scheduler-supported single-loop in v7.7 — see Known Issues).
- **Canvas blank after task click**: removed the `runtimeConnected` gate from the editor's data-fetch effects (`fetchTasks`, `fetchRuns`, run selection, NE fetch). HTTP fetches no longer wait for the WebSocket handshake; the canvas hydrates immediately from REST.
- **Chat session title shows the auto-generated counter (e.g. `Session 14`) instead of the task title**: `handleSelectSession` now overrides the stored title with the caller-supplied `fallbackTitle` whenever the stored title is empty or matches the auto-generated counter pattern. User-renamed sessions are preserved.
- **Pipeline node count regression**: `summaryCompletedNodes` now scopes to `activeRun.id` only (was double-gated on `selectedRun.id`).

### Internal
- `useTaskManager` and `useRunManager` validate effects gated by `loading && tasks.length > 0` so the URL `?task=X` handler isn't reset on first paint.
- WebSocket event filter now reads task ID from `msg.task_id`, `msg.payload.task_id`, `msg.payload.task.id`, plus three other fallback paths — fixes cross-task event leakage between sequential runs.
- Deterministic privacy mask (no UUIDs) so the same prompt produces the same redacted output across reruns.

### Known issues
- **Reviewer Reject Budget UI**: only `1` is functionally distinct in v7.7. Higher values (2/3/5) are saved but produce identical behavior because the v7.1k.2 patcher post-exec re-audit path is still gated. The full multi-round audit loop returns in v7.8 along with a scheduler fix that prevents PENDING-deadlocks.
- **Plan-timing**: `pro_template_profile` is computed against the user's raw prompt, not the enriched goal. A vague brief like "make a PvZ" still routes to the bare 12-node pipeline; expanding the prompt with explicit asset hints triggers the right path. v7.8 defers plan baking until after enrichment.
- **TCC permission popups repeat per rebuild**: ad-hoc signed builds have no stable identifier. Use `EVERMIND_CODESIGN_IDENTITY="Evermind Local Dev"` (with a Keychain self-signed cert) to keep grants stable. See INSTALL.md.
- **Reviewer browser preflight**: a Playwright Chromium window flashes during the reviewer node. This is the deterministic visual QA preflight; it closes automatically. Document but does not affect output quality.
- **Backend pytest stale**: ~104 tests have stale assertions (e.g. expecting 12-node pro plans pre-patcher injection); core paths still pass. Cleanup in v7.8.

### Migration from v7.4 / v7.5 / v7.6 internal builds
Settings persist; no migration steps needed. If you were on v7.6 the Launchpad's *New Task* link target changed from `/editor` to `/editor?fresh=1` — this is transparent to the user.

---

## [v7.6] — 2026-04-26 — internal

8-round PvZ pro stress test. 2/8 fully green, surfaced merger-pre-copy and dashboard-builder parallel issues; led to fixes shipped in v7.7.

## [v7.5] — 2026-04-26 — internal

Three.js auto-injection fixed (3D libs no longer added to 2D games). `init(;` typo auto-fix. Per-task workspace isolation.

## [v7.4] — 2026-04-25 — internal

Release-readiness audit (3 opus agents in parallel). 22 fixes including PII path leak, TCC root cause, `0.0.0.0` binding default, harness pollution. Desktop sync + ad-hoc resigning script.

## [v7.1k] — 2026-04-26 — internal

Open-source preparation: 4 connected-green runs, 8 palette themes, GitHub/Chat verification, removed personal contamination from `.app`.

## [v7.0] / [v7.1j] — 2026-04-24 — internal

Visual gate SOFT-PASS path; reviewer-shipped + deployer-ok handoff so a soft-failed visual smoke doesn't kill an otherwise good run. Fixed asyncio deadlock when the deterministic gate failed and builder retried.

## [v6.7] — 2026-04-24 — internal

First 12/12 fully-green pro run (46m 38s). Single-loop closure: reviewer rejection budget tightened to 1, builder/merger/patcher/polisher iter caps shrunk, full-rewrite guard prevents builder thrash.

## [v6.4 — v6.6] — 2026-04-21 → 2026-04-23 — internal

Patcher-only repair loop (no builder rerun on reviewer reject). Merger pre-copy ROOT-FALLBACK. Comprehensive TCC stable signing. Speed-pass (NOOP merger fast-path), Patcher NO-PREAMBLE, k2.6 promotion.

## [v6.0] — 2026-04-19 — internal

12-provider plugin system, browser overlay extensions, chat agent browser tools, README/INSTALL/BUILD/LICENSE first drafts.

## [v3.5.1 — v5.x]

See `docs/` for the early-version archive. Highlights: AI report persistence, full-streaming relay, Monaco editor, YAML harness templates, 16-node prompt externalization, per-task workspace isolation.
