# Building Evermind from source

## Prerequisites

- **macOS** (Apple Silicon recommended; the build also works on Intel)
- **Node.js** ≥ 18 + npm
- **Python** 3.10–3.12 (the build script uses `python3`)
- **Xcode Command Line Tools** (for `codesign`): `xcode-select --install`

Optional:
- **Apple Developer ID** (for notarized builds) — set `EVERMIND_CODESIGN_IDENTITY` to your identity name.

## Clone

```bash
git clone https://github.com/Zhiyuan-511/Evermind.git
cd Evermind
```

## Install backend deps

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
deactivate
cd ..
```

(The packaged `.app` ships its own bundled Python deps; this step is only for running `python3 server.py` directly during development.)

## Install frontend deps

```bash
cd frontend
npm install
cd ..
```

## Install electron deps

```bash
cd electron
npm install
cd ..
```

## Build the `.app`

```bash
cd electron
npm run pack:desktop
```

This runs the full chain:
1. `build:frontend` — `next build` produces a Next.js standalone bundle.
2. `prepare:frontend-bundle` — copies `.next/standalone/` to `electron/.packaged/frontend-standalone/`.
3. `electron-builder --dir` — assembles `Evermind.app` under `electron/dist/mac-arm64/Evermind.app`.
4. `sign:app` — ad-hoc signs the bundle (set `EVERMIND_CODESIGN_IDENTITY` to use a real cert).
5. `sync:desktop` — copies the freshly built `.app` to `~/Desktop/Evermind.app` for convenience.

To produce a signed DMG:

```bash
cd electron
npm run dist
```

Output: `electron/dist/Evermind-<version>-arm64.dmg`.

## Run the dev stack (no packaging)

```bash
# Terminal A — backend
cd backend && python3 server.py

# Terminal B — frontend
cd frontend && npm run dev
# Open http://localhost:3000
```

Frontend talks to backend at `http://127.0.0.1:8765` (HTTP) and `ws://127.0.0.1:8765/ws` (WebSocket).

## Build env vars

| Var | Effect |
|-----|--------|
| `EVERMIND_CODESIGN_IDENTITY="Your Cert Name"` | Sign with that identity instead of ad-hoc |
| `EVERMIND_OUTPUT_DIR=/path/to/output` | Override task output dir (defaults to `/tmp/evermind_output`) |
| `EVERMIND_REVIEWER_MAX_REJECTIONS=N` | Force reviewer reject budget (overrides Settings UI) |
| `EVERMIND_MAX_RETRIES=N` | Force per-node max retries (overrides Settings UI) |
| `EVERMIND_THINKING_DEPTH=fast|deep` | Force speed/quality mode |
| `EVERMIND_BROWSER_HEADFUL=1` | Make Playwright preflight windows visible (debugging) |

## Tests

```bash
cd backend && python3 -m pytest tests/ -q
```

Note: as of v7.7, ~104 tests have stale assertions (templates expect 12 nodes but generate 13 after patcher injection). These will be cleaned up in v7.8. The orchestrator/server/ai_bridge core paths are still validated.

## Troubleshooting builds

| Error | Fix |
|-------|-----|
| `electron-builder: identity is set to null` | This is intentional for the open-source ad-hoc build. Set `EVERMIND_CODESIGN_IDENTITY` to override. |
| `python3 not found in PATH` | Ensure `python3 --version` works in the same shell where you run `npm run pack`. |
| `npm ERR! peer dep` on next build | We pin `next@16.x` and `react@19.x`. Don't bump unless you've verified Turbopack still produces a valid `standalone` bundle. |
| `Permission denied: codesign` | Run `xcode-select --install`; codesign ships with the CLT. |

## Repo layout

```
backend/         Python orchestrator + node implementations + LLM bridge
frontend/        Next.js 16 + React 19 UI (App Router)
electron/        Electron 33 wrapper + build scripts
docs/            Per-version notes and design memos
archive_*/       Historical snapshots (excluded from build)
```
