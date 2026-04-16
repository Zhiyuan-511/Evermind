# Evermind V5.0 UI Overhaul — Handoff Document

## Status: Dev mode VERIFIED working, Production standalone needs Monaco CDN fix

---

## What's Done (verified in dev mode via Preview screenshots)

### 1. Right Panel Resizable
- **File**: `frontend/src/app/editor/page.tsx`
- Drag handle (4px) on left edge of right panel
- Width range: 300-700px, persisted to localStorage (`evermind-rpw`)
- State: `rightPanelWidth`, handler: `handleResizeStart`

### 2. Node List Simplified (24 → 13)
- **File**: `frontend/src/components/Sidebar.tsx`
- Kept: planner, analyst, uidesign, builder, polisher, reviewer, tester, deployer, debugger, scribe, imagegen, spritesheet, assetimport
- Removed: router, monitor, all Tool nodes (shell/fileread/filewrite/screenshot/browser/gitops/uicontrol), bgremove, merger

### 3. Top Toolbar "文件" Button
- **File**: `frontend/src/components/Toolbar.tsx`
- 3-way toggle: 节点(blue) | 预览(green) | 文件(purple)
- Uses `onSetCanvasView` prop

### 4. File Mode (canvasView='files')
- **File**: `frontend/src/app/editor/page.tsx`
- When `canvasView === 'files'`: left sidebar → file tree, center → CodeEditorPanel
- `useRuntimeConnection.ts`: canvasView type extended to `'editor' | 'preview' | 'files'`
- Sidebar auto-switches via `forcedMode` prop

### 5. CodeEditorPanel with Monaco Editor
- **File**: `frontend/src/components/CodeEditorPanel.tsx`
- Uses `@monaco-editor/react` + `monaco-editor`
- Features: 30+ language highlighting, minimap, code folding, bracket pairs, indent guides
- Diff view: Monaco DiffEditor (side-by-side)
- Tab bar with file tabs, unsaved indicator, Cmd+S save
- Breadcrumb path display
- **DEV MODE: Works perfectly** (Monaco loads from CDN)
- **PRODUCTION STANDALONE: Monaco may not load** (CDN/worker issue in Electron)

### 6. Antigravity-style Explorer
- **File**: `frontend/src/components/FileExplorerPanel.tsx`
- Header: "EXPLORER" label + 4 icon buttons (📄 new file, 📁 new folder, 🔄 refresh, ⊟ collapse all)
- Compact folder selector with pill buttons
- Search bar
- VS Code-style tree items (22px height, indent guides, folder/file icons)

### 7. DirectChatPanel Redesign
- **File**: `frontend/src/components/DirectChatPanel.tsx`
- Clean message bubbles (user=right blue, AI=left dark)
- Input pinned to bottom with rounded card design
- Files Modified card when AI changes files
- Model selector in header

### 8. Backend File APIs
- **File**: `backend/server.py`
- `POST /api/workspace/mkdir` — create folder
- `POST /api/workspace/write` — save file
- `POST /api/workspace/delete` — delete file/folder
- `POST /api/workspace/rename` — rename file/folder
- All have path traversal protection via `_validate_workspace_path()`

---

## THE REMAINING PROBLEM: Monaco in Production Standalone

### Root Cause
Monaco Editor (`@monaco-editor/react`) loads its core from CDN (cdn.jsdelivr.net) by default. In **dev mode** (`npm run dev`), this works perfectly. In **production standalone** mode (Next.js `output: "standalone"`), Monaco's CDN loading appears to work but the **AMD loader conflicts with webpack's `define`** function:

1. webpack/turbopack defines `window.define` with `.amd` property
2. Monaco's `/vs/loader.js` checks `typeof define !== 'function' || !define.amd`
3. Since webpack's `define.amd` exists, Monaco's AMD loader **skips initialization**
4. `window.require` remains webpack's require, not Monaco's AMD require
5. Monaco's language tokenizers (workers) fail to load → no syntax highlighting

### Attempted Fixes (all failed in production)
1. **Custom MonacoEnvironment.getWorker** — workers resolved but AMD loader still conflicted
2. **Temporary delete window.define** before loading — Turbopack re-injects it
3. **PrismJS fallback** — `react-simple-code-editor` uses `color: inherit !important` on `<pre>`, killing token colors
4. **PrismJS !important override CSS** — worked logically but untested in production due to time
5. **Local Monaco files in public/** — files accessible but loader init still blocked

### Recommended Fix (not yet implemented)
**Option A**: Use `next.config.ts` to exclude Monaco from Turbopack bundling:
```ts
// next.config.ts
const nextConfig = {
  output: "standalone",
  turbopack: {
    resolveAlias: {
      // Prevent Turbopack from processing Monaco AMD loader
    }
  }
};
```

**Option B**: Create a standalone HTML page (`/monaco-editor.html`) that loads Monaco independently via `<script>` tags, then embed it as an `<iframe>` in the React app. This completely bypasses the webpack/AMD conflict.

**Option C**: Use `monaco-editor/esm/vs/editor/editor.api` (ESM build) instead of the AMD build. This requires webpack configuration but avoids the AMD loader entirely.

---

## Build & Deploy Process

```bash
# 1. Build frontend
cd frontend && npm run build

# 2. Sync to Evermind.app (CORRECT paths)
rsync -av --delete .next/standalone/frontend/ ~/Desktop/Evermind.app/Contents/Resources/frontend-standalone/
rsync -av .next/static/ ~/Desktop/Evermind.app/Contents/Resources/frontend-standalone/.next/static/

# 3. Sync backend
rsync -av backend/server.py ~/Desktop/Evermind.app/Contents/Resources/backend/

# 4. If app won't start after code changes, rebuild Electron:
cd electron && npx electron-builder --mac --arm64
cp -R dist/mac-arm64/Evermind.app ~/Desktop/Evermind.app
# Then repeat steps 2-3

# WARNING: DO NOT run `codesign --force --deep -s -` — it breaks Electron's internal framework signatures
```

## Dependencies Added
```json
{
  "@monaco-editor/react": "^4.7.0",
  "monaco-editor": "^0.55.1",
  "prismjs": "^1.30.0",
  "react-simple-code-editor": "^0.14.1"
}
```
(prismjs and react-simple-code-editor are unused, can be removed)

## Files Modified
| File | Changes |
|------|---------|
| `frontend/src/app/editor/page.tsx` | +resize handle, +canvasView='files', +file open/save callbacks |
| `frontend/src/components/CodeEditorPanel.tsx` | **Complete rewrite** — Monaco Editor |
| `frontend/src/components/DirectChatPanel.tsx` | Redesigned UI + files_modified |
| `frontend/src/components/FileExplorerPanel.tsx` | Antigravity Explorer style |
| `frontend/src/components/Sidebar.tsx` | Simplified nodes + forcedMode prop |
| `frontend/src/components/Toolbar.tsx` | 3-way canvas toggle |
| `frontend/src/components/ChatPanel.tsx` | Removed hardcoded width |
| `frontend/src/hooks/useRuntimeConnection.ts` | canvasView type extended |
| `backend/server.py` | +mkdir/write/delete/rename APIs |
