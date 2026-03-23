/**
 * Evermind Desktop — Electron Main Process
 * Spawns Python backend + Next.js server, loads the editor in a BrowserWindow.
 *
 * macOS compatibility: GUI apps inherit a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin).
 * We fix this by injecting common tool paths and trying to load the user's shell PATH.
 */

const { app, BrowserWindow, dialog, shell, ipcMain } = require('electron');
const { spawn, execSync } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');
const os = require('os');
const url = require('url');

// tree-kill for reliable child process cleanup
let treeKill;
try {
    treeKill = require('tree-kill');
} catch {
    // fallback — will use process.kill() directly
    treeKill = (pid, signal, cb) => {
        try { process.kill(pid, signal); } catch { }
        if (cb) cb();
    };
}

// ── Deep Link Queue ──
// macOS may fire open-url BEFORE app.whenReady(), so we queue the goal
// and inject it once the main window's React app is ready.
let pendingDeepLinkGoal = null;  // string | null

// ── Paths ──
const IS_DEV = !app.isPackaged;
const HOME = os.homedir();
const RESOURCES = IS_DEV
    ? path.join(__dirname, '..')   // dev: project root
    : process.resourcesPath;       // production: .app/Contents/Resources

const BACKEND_DIR = IS_DEV
    ? path.join(RESOURCES, 'backend')
    : path.join(RESOURCES, 'backend');

const FRONTEND_STANDALONE = IS_DEV
    ? path.join(RESOURCES, 'frontend', '.next', 'standalone')
    : path.join(RESOURCES, 'frontend-standalone');

const SPLASH_HTML = path.join(__dirname, 'splash.html');

// ── State ──
let mainWindow = null;
let splashWindow = null;
let backendProcess = null;
let frontendProcess = null;

const BACKEND_PORT = 8765;
const FRONTEND_PORT = 3000;

ipcMain.handle('evermind:reveal-in-finder', async (_event, targetPath) => {
    const resolvedPath = typeof targetPath === 'string' ? targetPath.trim() : '';
    if (!resolvedPath) return false;
    try {
        if (fs.existsSync(resolvedPath)) {
            shell.showItemInFolder(resolvedPath);
            return true;
        }
        const parentDir = path.dirname(resolvedPath);
        if (parentDir && fs.existsSync(parentDir)) {
            shell.openPath(parentDir);
            return true;
        }
        return false;
    } catch (error) {
        console.warn('[Electron] reveal-in-finder failed:', error);
        return false;
    }
});

// ═══════════════════════════════════════════
// macOS PATH Fix — CRITICAL for packaged .app
// ═══════════════════════════════════════════
function fixMacOSPath() {
    // macOS GUI apps get a minimal PATH: /usr/bin:/bin:/usr/sbin:/sbin
    // We add all common tool directories using ONLY filesystem checks (no shell calls)
    const extraPaths = [
        '/opt/homebrew/bin',           // Homebrew (Apple Silicon)
        '/opt/homebrew/sbin',
        '/usr/local/bin',              // Homebrew (Intel) / manual installs
        '/usr/local/sbin',
        '/usr/sbin',                   // lsof lives here
        path.join(HOME, '.local/bin'), // pip --user installs
    ];

    // Python.org framework (macOS installer)
    const frameworkBase = '/Library/Frameworks/Python.framework/Versions';
    if (fs.existsSync(frameworkBase)) {
        try {
            const versions = fs.readdirSync(frameworkBase)
                .filter(v => /^\d+\.\d+$/.test(v))
                .sort()
                .reverse();
            for (const v of versions) {
                extraPaths.push(path.join(frameworkBase, v, 'bin'));
            }
        } catch { /* ignore */ }
    }

    // pyenv
    const pyenvRoot = process.env.PYENV_ROOT || path.join(HOME, '.pyenv');
    extraPaths.push(path.join(pyenvRoot, 'shims'));
    extraPaths.push(path.join(pyenvRoot, 'bin'));

    // nvm — find latest installed Node version
    const nvmDir = process.env.NVM_DIR || path.join(HOME, '.nvm');
    const nvmVersions = path.join(nvmDir, 'versions', 'node');
    if (fs.existsSync(nvmVersions)) {
        try {
            const versions = fs.readdirSync(nvmVersions)
                .filter(v => v.startsWith('v'))
                .sort()
                .reverse();
            for (const v of versions) {
                extraPaths.push(path.join(nvmVersions, v, 'bin'));
            }
        } catch { /* ignore */ }
    }

    // Volta
    extraPaths.push(path.join(HOME, '.volta', 'bin'));

    // Conda
    extraPaths.push(
        path.join(HOME, 'anaconda3', 'bin'),
        path.join(HOME, 'miniconda3', 'bin'),
        '/opt/anaconda3/bin',
        '/opt/miniconda3/bin',
    );

    // Merge with existing PATH, remove duplicates — NO shell calls needed
    const currentPaths = (process.env.PATH || '').split(':');
    const allPaths = [...new Set([...extraPaths, ...currentPaths])].filter(Boolean);
    process.env.PATH = allPaths.join(':');

    console.log('[Electron] Fixed PATH for macOS (filesystem-only, instant)');
}

// ───────────────────────────────────────────
// Kill any zombie process on a port
// ───────────────────────────────────────────
function killPortProcess(port) {
    try {
        // Use absolute path for lsof since PATH might not include /usr/sbin
        const lsofCmd = fs.existsSync('/usr/sbin/lsof') ? '/usr/sbin/lsof' : 'lsof';
        const pids = execSync(`${lsofCmd} -ti :${port}`, { encoding: 'utf8' }).trim();
        if (pids) {
            for (const pid of pids.split('\n')) {
                try {
                    process.kill(Number(pid), 'SIGKILL');
                    console.log(`[Electron] Killed zombie process ${pid} on port ${port}`);
                } catch { /* already dead */ }
            }
            // Give OS a moment to release the port
            execSync('sleep 0.5');
        }
    } catch {
        // No process on this port — good
    }
}

// ───────────────────────────────────────────
// Check if a Python interpreter has our deps
// ───────────────────────────────────────────
function pythonHasDeps(pythonPath) {
    try {
        execSync(`"${pythonPath}" -c "import fastapi; import uvicorn; import dotenv"`, {
            stdio: 'ignore', timeout: 10000,
        });
        return true;
    } catch {
        return false;
    }
}

// ───────────────────────────────────────────
// Find Python3 — prefers the one with deps
// ───────────────────────────────────────────
function findPython() {
    // Build candidate list: Python.org framework FIRST (most likely to have packages),
    // then conda, pyenv, then Homebrew/system LAST (PEP 668 may block pip).
    const candidates = [];

    // 1. Python.org framework installs (best choice — pip works without restrictions)
    const frameworkBase = '/Library/Frameworks/Python.framework/Versions';
    if (fs.existsSync(frameworkBase)) {
        try {
            const versions = fs.readdirSync(frameworkBase)
                .filter(v => /^\d+\.\d+$/.test(v))
                .sort()
                .reverse(); // newest first
            for (const v of versions) {
                candidates.push(path.join(frameworkBase, v, 'bin', 'python3'));
            }
        } catch { /* ignore */ }
    }

    // 2. Conda
    candidates.push(
        '/opt/anaconda3/bin/python3',
        path.join(HOME, 'anaconda3', 'bin', 'python3'),
        path.join(HOME, 'miniconda3', 'bin', 'python3'),
        '/opt/miniconda3/bin/python3',
    );

    // 3. pyenv
    const pyenvRoot = process.env.PYENV_ROOT || path.join(HOME, '.pyenv');
    candidates.push(path.join(pyenvRoot, 'shims', 'python3'));

    // 4. Homebrew / system (may have PEP 668 restrictions)
    candidates.push(
        '/opt/homebrew/bin/python3',    // Homebrew Apple Silicon
        '/usr/local/bin/python3',       // Homebrew Intel
        '/usr/bin/python3',             // macOS system
    );

    // Phase 1: Find a Python that ALREADY has our dependencies
    for (const p of candidates) {
        if (fs.existsSync(p)) {
            try {
                const version = execSync(`"${p}" --version 2>&1`, { encoding: 'utf8' }).trim();
                if (version.includes('3.')) {
                    if (pythonHasDeps(p)) {
                        console.log(`[Electron] Found Python with deps: ${p} → ${version}`);
                        return p;
                    }
                    console.log(`[Electron] Python ${p} (${version}) — missing deps, trying next`);
                }
            } catch { /* try next */ }
        }
    }

    // Phase 2: No Python with deps found — return first valid Python3
    // (ensurePythonDeps will try to install packages)
    for (const p of candidates) {
        if (fs.existsSync(p)) {
            try {
                const version = execSync(`"${p}" --version 2>&1`, { encoding: 'utf8' }).trim();
                if (version.includes('3.')) {
                    console.log(`[Electron] Found Python (no deps yet): ${p} → ${version}`);
                    return p;
                }
            } catch { /* try next */ }
        }
    }

    // Phase 3: PATH-based fallback (dev mode)
    for (const cmd of ['python3', 'python']) {
        try {
            const version = execSync(`${cmd} --version 2>&1`, { encoding: 'utf8' }).trim();
            if (version.includes('3.')) {
                console.log(`[Electron] Found Python via PATH: ${cmd} → ${version}`);
                return cmd;
            }
        } catch { /* not found */ }
    }

    console.error('[Electron] Python3 not found. Searched:', candidates.join('\n  '));
    return null;
}

// ───────────────────────────────────────────
// Find Node binary — uses absolute paths first
// ───────────────────────────────────────────
function findNode() {
    // 1. Check well-known absolute paths
    const absolutePaths = [
        '/opt/homebrew/bin/node',       // Homebrew Apple Silicon
        '/usr/local/bin/node',          // Homebrew Intel / nvm legacy
    ];

    // nvm versions
    const nvmDir = process.env.NVM_DIR || path.join(HOME, '.nvm');
    const nvmVersions = path.join(nvmDir, 'versions', 'node');
    if (fs.existsSync(nvmVersions)) {
        try {
            const versions = fs.readdirSync(nvmVersions)
                .filter(v => v.startsWith('v'))
                .sort()
                .reverse();
            for (const v of versions) {
                absolutePaths.push(path.join(nvmVersions, v, 'bin', 'node'));
            }
        } catch { /* ignore */ }
    }

    // Volta
    absolutePaths.push(path.join(HOME, '.volta', 'bin', 'node'));

    for (const p of absolutePaths) {
        if (fs.existsSync(p)) {
            console.log(`[Electron] Found Node: ${p}`);
            return p;
        }
    }

    // 2. Fallback: try which
    try {
        const nodePath = execSync('which node', { encoding: 'utf8' }).trim();
        if (nodePath) {
            console.log(`[Electron] Found Node via which: ${nodePath}`);
            return nodePath;
        }
    } catch { }

    console.warn('[Electron] Node not found at known paths, using "node" fallback');
    return 'node'; // fallback
}

// ───────────────────────────────────────────
// Wait for a service to be ready
// ───────────────────────────────────────────
function waitForService(port, label, timeoutMs = 90000) {
    return new Promise((resolve, reject) => {
        const start = Date.now();
        const check = () => {
            const req = http.get(`http://127.0.0.1:${port}`, (res) => {
                res.resume();
                console.log(`[Electron] ${label} ready on port ${port} (${((Date.now() - start) / 1000).toFixed(1)}s)`);
                resolve();
            });
            req.on('error', () => {
                if (Date.now() - start > timeoutMs) {
                    reject(new Error(`${label} 未能在 ${timeoutMs / 1000} 秒内启动`));
                } else {
                    setTimeout(check, 800);
                }
            });
            req.setTimeout(3000, () => { req.destroy(); });
        };
        check();
    });
}

// ───────────────────────────────────────────
// Install Python dependencies if needed
// ───────────────────────────────────────────
function ensurePythonDeps(pythonCmd) {
    const reqFile = path.join(BACKEND_DIR, 'requirements.txt');
    if (!fs.existsSync(reqFile)) return;

    // Already have deps? Skip.
    if (pythonHasDeps(pythonCmd)) {
        console.log('[Electron] Python dependencies already installed');
        return;
    }

    console.log('[Electron] Installing Python dependencies...');

    // Try normal pip install first
    try {
        execSync(
            `"${pythonCmd}" -m pip install --user -r "${reqFile}" --quiet`,
            { stdio: 'inherit', timeout: 180000 }
        );
        console.log('[Electron] Python dependencies installed successfully');
        return;
    } catch (err) {
        console.warn('[Electron] Normal pip install failed, trying --break-system-packages...');
    }

    // Fallback for PEP 668 externally-managed environments (Homebrew Python)
    try {
        execSync(
            `"${pythonCmd}" -m pip install --user --break-system-packages -r "${reqFile}" --quiet`,
            { stdio: 'inherit', timeout: 180000 }
        );
        console.log('[Electron] Python dependencies installed (break-system-packages)');
        return;
    } catch (err) {
        console.error('[Electron] Failed to install Python deps:', err.message);
    }
}

// ───────────────────────────────────────────
// Start Python Backend
// ───────────────────────────────────────────
function startBackend(pythonCmd) {
    console.log(`[Electron] Starting backend in: ${BACKEND_DIR}`);
    console.log(`[Electron] Backend command: ${pythonCmd} server.py`);

    const env = {
        ...process.env,
        HOST: '127.0.0.1',
        PORT: String(BACKEND_PORT),
        WORKSPACE: path.join(HOME, 'Desktop'),
        OUTPUT_DIR: path.join(app.getPath('temp'), 'evermind_output'),
        ALLOWED_DIRS: [
            path.join(HOME, 'Desktop'),
            path.join(HOME, 'Documents'),
            path.join(app.getPath('temp'), 'evermind_output'),
            '/tmp',
        ].join(','),
        SHELL_TIMEOUT: '30',
        // Ensure Python can find user-installed packages
        PYTHONPATH: BACKEND_DIR,
    };

    // Ensure output dir exists
    const outputDir = env.OUTPUT_DIR;
    if (!fs.existsSync(outputDir)) {
        fs.mkdirSync(outputDir, { recursive: true });
    }

    backendProcess = spawn(pythonCmd, ['server.py'], {
        cwd: BACKEND_DIR,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: false,
    });

    backendProcess.stdout.on('data', (d) => console.log(`[Backend] ${d.toString().trim()}`));
    backendProcess.stderr.on('data', (d) => console.error(`[Backend] ${d.toString().trim()}`));
    backendProcess.on('error', (err) => {
        console.error(`[Backend] Failed to start: ${err.message}`);
    });
    backendProcess.on('exit', (code, signal) => {
        console.log(`[Backend] Exited with code ${code}, signal ${signal}`);
        backendProcess = null;
    });
}

// ───────────────────────────────────────────
// Start Next.js Frontend
// ───────────────────────────────────────────
function startFrontend() {
    const standaloneServer = path.join(FRONTEND_STANDALONE, 'server.js');
    const hasStandalone = fs.existsSync(standaloneServer);

    let cmd, args, cwd;

    if (hasStandalone) {
        console.log('[Electron] Starting frontend (standalone build)');
        console.log(`[Electron] Standalone server: ${standaloneServer}`);
        cmd = findNode();
        args = [standaloneServer];
        cwd = FRONTEND_STANDALONE;
    } else if (IS_DEV) {
        console.log('[Electron] Starting frontend (dev mode)');
        cmd = 'npx';
        args = ['next', 'dev'];
        cwd = path.join(RESOURCES, 'frontend');
    } else {
        dialog.showErrorBox(
            '前端构建缺失',
            'Evermind 桌面版需要 Next.js standalone 构建。\n\n'
            + '请在项目目录中运行：\n'
            + '  cd frontend && npm run build\n\n'
            + '然后重新打包 Electron 应用。'
        );
        app.quit();
        return;
    }

    const env = {
        ...process.env,
        PORT: String(FRONTEND_PORT),
        HOSTNAME: '0.0.0.0',
        NODE_ENV: hasStandalone ? 'production' : 'development',
        NEXT_PUBLIC_API_URL: `http://127.0.0.1:${BACKEND_PORT}`,
        NEXT_PUBLIC_WS_URL: `ws://127.0.0.1:${BACKEND_PORT}/ws`,
    };

    frontendProcess = spawn(cmd, args, {
        cwd,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
        shell: !hasStandalone, // only use shell for npx (dev mode)
    });

    frontendProcess.stdout.on('data', (d) => console.log(`[Frontend] ${d.toString().trim()}`));
    frontendProcess.stderr.on('data', (d) => {
        const msg = d.toString().trim();
        if (!msg.includes('Compiling') && !msg.includes('warn')) {
            console.error(`[Frontend] ${msg}`);
        }
    });
    frontendProcess.on('error', (err) => {
        console.error(`[Frontend] Failed to start: ${err.message}`);
    });
    frontendProcess.on('exit', (code, signal) => {
        console.log(`[Frontend] Exited with code ${code}, signal ${signal}`);
        frontendProcess = null;
    });
}

// ───────────────────────────────────────────
// Show Splash Screen
// ───────────────────────────────────────────
function showSplash() {
    splashWindow = new BrowserWindow({
        width: 420,
        height: 320,
        frame: false,
        transparent: true,
        resizable: false,
        center: true,
        alwaysOnTop: true,
        skipTaskbar: true,
        webPreferences: { nodeIntegration: false, contextIsolation: true },
    });
    splashWindow.loadFile(SPLASH_HTML);
}

// ───────────────────────────────────────────
// Update splash status message
// ───────────────────────────────────────────
function updateSplashStatus(msg, progress) {
    if (splashWindow && !splashWindow.isDestroyed()) {
        const safeMsg = JSON.stringify(String(msg || ''));
        const progressScript = typeof progress === 'number'
            ? `const progressEl = document.getElementById('progress-bar'); if (progressEl) progressEl.style.width = '${Math.max(0, Math.min(100, progress))}%';`
            : '';
        splashWindow.webContents.executeJavaScript(
            `
                const statusEl = document.getElementById('status-text');
                if (statusEl) statusEl.textContent = ${safeMsg};
                ${progressScript}
            `
        ).catch(() => { });
    }
}

// ───────────────────────────────────────────
// Create Main Window
// ───────────────────────────────────────────
function createMainWindow() {
    mainWindow = new BrowserWindow({
        width: 1400,
        height: 900,
        minWidth: 900,
        minHeight: 600,
        title: IS_DEV ? 'Evermind (DEV)' : 'Evermind Desktop',
        titleBarStyle: 'hiddenInset',
        trafficLightPosition: { x: 15, y: 15 },
        backgroundColor: '#0f1117',
        show: false,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            preload: path.join(__dirname, 'preload.js'),
        },
    });

    // §2.1: Pass instance type to frontend so UI can display DEV/PACKAGED badge
    const envTag = IS_DEV ? 'dev' : 'packaged';
    mainWindow.loadURL(`http://127.0.0.1:${FRONTEND_PORT}/editor?env=${envTag}`);

    mainWindow.once('ready-to-show', () => {
        if (splashWindow && !splashWindow.isDestroyed()) {
            splashWindow.close();
            splashWindow = null;
        }
        mainWindow.show();
        mainWindow.focus();
    });

    // ── Deep Link: inject queued goal after page fully loads ──
    mainWindow.webContents.on('did-finish-load', () => {
        console.log('[Electron] Main window did-finish-load');
        if (pendingDeepLinkGoal) {
            console.log(`[Electron] Injecting queued deep link goal: ${pendingDeepLinkGoal}`);
            // Wait for React to mount and register event listeners
            _injectDeepLinkGoal(pendingDeepLinkGoal);
            pendingDeepLinkGoal = null;
        }
    });

    // Open external links in browser
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        if (url.startsWith('http')) shell.openExternal(url);
        return { action: 'deny' };
    });

    mainWindow.on('closed', () => { mainWindow = null; });
}

// ───────────────────────────────────────────
// App Lifecycle
// ───────────────────────────────────────────
app.whenReady().then(async () => {
    // 0. Fix PATH for macOS — MUST be first
    fixMacOSPath();

    // 0.5. Register evermind:// protocol so OpenClaw can launch the app directly
    if (!app.isDefaultProtocolClient('evermind')) {
        app.setAsDefaultProtocolClient('evermind');
        console.log('[Electron] Registered evermind:// protocol');
    }

    // 1. Show splash
    showSplash();

    // 1.5. Kill any zombie processes on our ports
    updateSplashStatus('正在清理端口...', 10);
    killPortProcess(BACKEND_PORT);
    killPortProcess(FRONTEND_PORT);

    // 2. Find Python
    updateSplashStatus('正在检查 Python...', 24);
    const pythonCmd = findPython();
    if (!pythonCmd) {
        dialog.showErrorBox(
            'Python 未安装',
            'Evermind 需要 Python 3.10+ 来运行 AI 后端。\n\n'
            + '请安装 Python：\n'
            + '• macOS: brew install python3\n'
            + '• 或者从 https://python.org 下载\n\n'
            + '安装后重新打开 Evermind。'
        );
        app.quit();
        return;
    }

    // 3. Install deps if needed
    updateSplashStatus('正在检查依赖...', 38);
    ensurePythonDeps(pythonCmd);

    // 4. Start services
    try {
        updateSplashStatus('正在启动后端服务...', 56);
        startBackend(pythonCmd);
        await waitForService(BACKEND_PORT, 'Backend', 90000);

        updateSplashStatus('正在启动前端服务...', 76);
        startFrontend();
        await waitForService(FRONTEND_PORT, 'Frontend', 90000);

        // 5. Show main window
        updateSplashStatus('正在加载编辑器...', 92);
        createMainWindow();
    } catch (err) {
        console.error('[Electron] Startup error:', err);
        dialog.showErrorBox(
            '启动失败',
            `Evermind 服务启动失败：\n\n${err.message}\n\n`
            + '请检查：\n'
            + '1. Python3 是否已安装 (python3 --version)\n'
            + '2. Python 依赖是否已安装 (pip install -r requirements.txt)\n'
            + '3. Node.js 是否已安装 (node --version)\n\n'
            + `当前 PATH:\n${process.env.PATH.split(':').slice(0, 10).join('\n')}`
        );
        cleanup();
        app.quit();
    }
});

// macOS: re-create window when clicking dock icon
app.on('activate', () => {
    if (!mainWindow) createMainWindow();
});

// ── Deep Link: inject goal into frontend React app ──
// Retries up to 3 times with 2s delays to wait for React mounting
function _injectDeepLinkGoal(goal, attempt = 0) {
    if (!goal || !mainWindow || mainWindow.isDestroyed()) {
        console.warn(`[DeepLink] Cannot inject: goal=${!!goal} mainWindow=${!!mainWindow}`);
        return;
    }
    const maxAttempts = 3;
    const retryDelay = 2000; // ms
    const jsCode = `
        (function() {
            console.log('[DeepLink] Injecting goal (attempt ${attempt + 1}/${maxAttempts}): ' + ${JSON.stringify(goal)});
            window.__EVERMIND_DEEPLINK_GOAL = ${JSON.stringify(goal)};
            window.dispatchEvent(new CustomEvent('evermind-deeplink', { detail: { goal: ${JSON.stringify(goal)} } }));
            return 'dispatched';
        })()
    `;
    mainWindow.webContents.executeJavaScript(jsCode)
        .then((result) => {
            console.log(`[DeepLink] executeJavaScript result (attempt ${attempt + 1}): ${result}`);
            // Schedule a retry in case React hasn't mounted its listener yet
            if (attempt < maxAttempts - 1) {
                setTimeout(() => {
                    // Only retry if the goal hasn't been consumed yet
                    if (!mainWindow || mainWindow.isDestroyed()) return;
                    mainWindow.webContents.executeJavaScript(
                        `window.__EVERMIND_DEEPLINK_GOAL`
                    ).then((val) => {
                        if (val && val === goal) {
                            console.log(`[DeepLink] Goal still pending, retrying (attempt ${attempt + 2})...`);
                            _injectDeepLinkGoal(goal, attempt + 1);
                        } else {
                            console.log(`[DeepLink] Goal was consumed or changed, no retry needed`);
                        }
                    }).catch(() => {
                        // Page might have navigated, retry anyway
                        _injectDeepLinkGoal(goal, attempt + 1);
                    });
                }, retryDelay);
            }
        })
        .catch((err) => {
            console.error(`[DeepLink] executeJavaScript failed (attempt ${attempt + 1}):`, err.message);
            if (attempt < maxAttempts - 1) {
                setTimeout(() => _injectDeepLinkGoal(goal, attempt + 1), retryDelay);
            }
        });
}

// macOS: handle evermind:// deep link (OpenClaw or other apps)
// NOTE: This event can fire BEFORE app.whenReady() on cold start!
app.on('open-url', (event, deepLinkUrl) => {
    event.preventDefault();
    console.log(`[Electron] open-url received: ${deepLinkUrl}`);
    console.log(`[Electron] App state: mainWindow=${!!mainWindow}, isReady=${app.isReady()}`);

    // Parse goal from URL
    let goal = null;
    try {
        const parsed = new URL(deepLinkUrl);
        // evermind://run?goal=... → hostname='run', searchParams has 'goal'
        // evermind:///run?goal=... → pathname='/run', searchParams has 'goal'
        if (parsed.hostname === 'run' || parsed.pathname === '/run' || parsed.pathname === 'run') {
            goal = parsed.searchParams.get('goal');
        }
    } catch (e) {
        console.warn('[Electron] Failed to parse deep link URL:', e.message);
    }

    console.log(`[Electron] Parsed deep link goal: ${goal ? goal.substring(0, 80) : '(none)'}`);

    if (goal) {
        // Queue the goal — it will be injected when the window is ready
        pendingDeepLinkGoal = goal;

        if (mainWindow && !mainWindow.isDestroyed()) {
            // App is already running — bring to front and inject immediately
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.focus();
            _injectDeepLinkGoal(goal);
        } else {
            console.log('[Electron] mainWindow not ready, goal queued for did-finish-load');
            // Goal will be injected by the did-finish-load handler in createMainWindow
        }
    } else {
        // No goal — just bring window to front
        if (mainWindow && !mainWindow.isDestroyed()) {
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.focus();
        }
    }
});

// ── Cleanup ──
function cleanup() {
    console.log('[Electron] Cleaning up...');
    if (backendProcess && backendProcess.pid) {
        treeKill(backendProcess.pid, 'SIGTERM', (err) => {
            if (err) console.error('[Electron] Failed to kill backend:', err);
        });
        backendProcess = null;
    }
    if (frontendProcess && frontendProcess.pid) {
        treeKill(frontendProcess.pid, 'SIGTERM', (err) => {
            if (err) console.error('[Electron] Failed to kill frontend:', err);
        });
        frontendProcess = null;
    }
}

app.on('before-quit', cleanup);
app.on('window-all-closed', () => {
    cleanup();
    app.quit();
});

process.on('SIGINT', () => { cleanup(); process.exit(0); });
process.on('SIGTERM', () => { cleanup(); process.exit(0); });
