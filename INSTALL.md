# Installing Evermind

## Quick start (macOS, Apple Silicon)

1. Download the latest `Evermind-*.dmg` from the [Releases page](https://github.com/Zhiyuan-511/Evermind/releases).
2. Open the DMG and drag `Evermind.app` to **Applications** (or any folder).
3. **First launch**: macOS will show a security warning ("cannot verify the developer"). Right-click `Evermind.app` → **Open** → confirm. This is a one-time step for the ad-hoc-signed open-source build.
4. The first time you run any node, macOS will request **Files & Folders / Network** permissions. Grant them. (See [Permission popup notes](#permission-popups) below.)

## API keys

Evermind needs at least one LLM API key to actually plan/build. Open the app → **Settings → API Keys** and add one of:

- **OpenAI** (`sk-...`) — works at `api.openai.com/v1` by default. If you use a relay (relay / relay / etc.) override the **API base** field.
- **Anthropic** (`sk-ant-...`) — Claude models.
- **Google** (Gemini API key).
- **Kimi / Moonshot** — `sk-...` from `platform.moonshot.cn` (or `kimi-coding` plan from `api.kimi.com/coding/v1`).
- **Zhipu (GLM)**, **Qwen**, **DeepSeek**, **MiniMax**, **Doubao** — see Settings dropdown.

You can also point the whole stack at a single relay (`relay` style) — set the relay base URL in **Settings → Relays**.

## Build from source

If you'd rather build the `.app` yourself instead of downloading the DMG, see [BUILD.md](BUILD.md).

## Permission popups

**Why does macOS ask for permission every time I rebuild?** The open-source build is **ad-hoc signed** (no Apple Developer ID). macOS treats every rebuild as a brand-new app and resets TCC permissions. Two workarounds:

1. **Self-sign once** (recommended for active developers): create a self-signed cert in Keychain Access (Certificate Assistant → Create), then run:
   ```bash
   export EVERMIND_CODESIGN_IDENTITY="Evermind Local Dev"
   cd electron && npm run sign:app
   ```
   TCC will then keep the permissions across rebuilds.
2. **Just accept it** — for occasional users this is fine; the permissions you grant are local to your machine.

If you have an Apple Developer account ($99/yr), you can pass your Developer ID identity via the same env var and produce a notarizable build.

## Reviewer browser preflight

When the **Reviewer** node runs, Evermind opens a hidden Chromium window via Playwright to QA your build (visual smoke test). You may see a brief flash in the macOS dock — this is expected; the window closes automatically when the QA preflight finishes.

## Where things live

| Path | What |
|------|------|
| `~/.evermind/config.json` | API keys (Fernet-encrypted), node preferences |
| `~/.evermind/.key` | Fernet symmetric key (filesystem-protected; back this up to keep your saved keys) |
| `~/.evermind/workspaces/<task-id>/` | Per-task working directory |
| `/tmp/evermind_output/_stable_previews/run_<id>/.../index.html` | Stable preview snapshots from each run |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Connection refused 127.0.0.1:8765` | Backend didn't start | Check Console.app for `[evermind.server]` errors; usually a missing Python dep — re-run via `cd backend && python3 server.py` to see the real traceback |
| Chat agent says `model X 不存在 400` | Relay doesn't host that model | Settings → switch `default_model` to `gpt-5.4-mini` / `kimi-k2.5` |
| TCC popup repeats | Ad-hoc signature reset | See [Permission popups](#permission-popups) above |
| First task is very slow | LLM relay cold-start | Subsequent runs reuse caches and are 3-10× faster |
| Builder writes 1 line of code | Stream stalled mid-flight | Cancel the run and retry; if persistent see `~/.evermind/logs/evermind-backend.log` |

## Uninstall

```bash
rm -rf /Applications/Evermind.app ~/.evermind /tmp/evermind_output ~/Library/Application\ Support/evermind-desktop
```

This removes the app, your saved API keys, all task workspaces, and Electron's local storage.
