const { app, BrowserWindow, ipcMain } = require('electron');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const DEFAULT_WIDTH = 1280;
const DEFAULT_HEIGHT = 720;
const DEFAULT_SETTLE_MS = 1200;
const DEFAULT_TIMEOUT_MS = 18000;
const DEFAULT_KEEP_OPEN_MS = 8000;
const SHOWCASE_DELAY_MS = 800;
const TIMELAPSE_INTERVAL_MS = 500;
const RRWEB_CDN_URL = 'https://cdn.jsdelivr.net/npm/rrweb@2.0.0-alpha.17/dist/rrweb.umd.cjs.js';
const DEFAULT_KEY_SEQUENCE = ['ArrowUp', 'ArrowRight', 'Space', 'ArrowLeft', 'ArrowDown'];

function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function sanitizeSegment(value, fallback = 'session') {
    const text = String(value || '').trim().replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
    return text || fallback;
}

function hashBuffer(buffer) {
    return crypto.createHash('sha1').update(buffer).digest('hex').slice(0, 16);
}

function ensureDir(targetDir) {
    fs.mkdirSync(targetDir, { recursive: true });
    return targetDir;
}

function writeJson(targetPath, payload) {
    fs.writeFileSync(targetPath, JSON.stringify(payload, null, 2), 'utf8');
}

function waitForPageReady(webContents, timeoutMs = DEFAULT_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
        let settled = false;
        let domReadySeen = false;
        const cleanup = () => {
            webContents.removeListener('did-finish-load', onDone);
            webContents.removeListener('did-stop-loading', onStop);
            webContents.removeListener('dom-ready', onDomReady);
            webContents.removeListener('did-fail-load', onFail);
            clearInterval(pollTimer);
            clearTimeout(timer);
        };
        const finish = (fn, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            fn(value);
        };
        const onDone = () => finish(resolve, { mode: 'did-finish-load', partial: false });
        const onStop = () => finish(resolve, { mode: 'did-stop-loading', partial: false });
        const onDomReady = () => {
            domReadySeen = true;
            finish(resolve, { mode: 'dom-ready', partial: false });
        };
        const onFail = (_event, code, description, validatedURL, isMainFrame) => {
            if (!isMainFrame) return;
            finish(reject, new Error(`Failed to load ${validatedURL || 'preview'}: ${description || code}`));
        };
        const timer = setTimeout(async () => {
            try {
                const snapshot = await webContents.executeJavaScript(`
                    (() => ({
                        readyState: document.readyState || '',
                        hasBody: Boolean(document.body),
                        textLength: Number((document.body?.innerText || '').trim().length || 0),
                        canvasCount: Number(document.querySelectorAll('canvas').length || 0),
                    }))();
                `, true);
                const hasRenderableBody = Boolean(snapshot?.hasBody) && (
                    String(snapshot?.readyState || '') !== 'loading'
                    || Number(snapshot?.textLength || 0) > 0
                    || Number(snapshot?.canvasCount || 0) > 0
                    || domReadySeen
                );
                if (hasRenderableBody) {
                    finish(resolve, { mode: 'timeout-partial', partial: true });
                    return;
                }
            } catch {
                // Fall through to the hard timeout below.
            }
            finish(reject, new Error(`QA preview load timed out after ${timeoutMs}ms`));
        }, timeoutMs);
        const pollTimer = setInterval(async () => {
            if (settled) return;
            try {
                const readyState = await webContents.executeJavaScript('document.readyState', true);
                if (String(readyState || '') === 'interactive' || String(readyState || '') === 'complete') {
                    finish(resolve, { mode: 'ready-state', partial: false });
                }
            } catch {
                // Ignore polling failures while the page is still initializing.
            }
        }, 250);
        if (!webContents.isLoading()) {
            finish(resolve, { mode: 'already-ready', partial: false });
            return;
        }
        webContents.once('did-finish-load', onDone);
        webContents.once('did-stop-loading', onStop);
        webContents.once('dom-ready', onDomReady);
        webContents.on('did-fail-load', onFail);
    });
}

async function installPageErrorHooks(webContents) {
    try {
        await webContents.executeJavaScript(`
            (() => {
                if (window.__evermindQaState) return true;
                const state = {
                    pageErrors: [],
                    rejections: [],
                    markers: [],
                };
                window.__evermindQaState = state;
                window.addEventListener('error', (event) => {
                    state.pageErrors.push({
                        message: String(event?.message || 'unknown error'),
                        source: String(event?.filename || ''),
                        lineno: Number(event?.lineno || 0),
                        colno: Number(event?.colno || 0),
                    });
                });
                window.addEventListener('unhandledrejection', (event) => {
                    state.rejections.push({
                        reason: String(event?.reason || 'unhandled rejection'),
                    });
                });
                return true;
            })();
        `, true);
    } catch {
        // Ignore hook injection failures on restrictive pages.
    }
}

async function installRrwebRecording(webContents) {
    try {
        await webContents.executeJavaScript(`
            (() => {
                if (window.__evermindRrwebEvents) return true;
                window.__evermindRrwebEvents = [];
                window.__evermindRrwebReady = false;
                const script = document.createElement('script');
                script.src = ${JSON.stringify(RRWEB_CDN_URL)};
                script.onload = () => {
                    try {
                        const rrwebLib = window.rrweb || window.rrwebRecord;
                        const recordFn = rrwebLib && (rrwebLib.record || rrwebLib.default?.record || rrwebLib);
                        if (typeof recordFn === 'function') {
                            recordFn({
                                emit(event) {
                                    if (window.__evermindRrwebEvents.length < 5000) {
                                        window.__evermindRrwebEvents.push(event);
                                    }
                                },
                            });
                            window.__evermindRrwebReady = true;
                        }
                    } catch (err) {
                        console.warn('[evermind-qa] rrweb record init failed:', err);
                    }
                };
                script.onerror = () => {
                    console.warn('[evermind-qa] rrweb CDN load failed');
                };
                document.head.appendChild(script);
                return true;
            })();
        `, true);
    } catch {
        // Ignore rrweb injection failures.
    }
}

async function collectRrwebEvents(webContents) {
    try {
        const events = await webContents.executeJavaScript(
            'Array.isArray(window.__evermindRrwebEvents) ? window.__evermindRrwebEvents : []',
            true,
        );
        return Array.isArray(events) ? events : [];
    } catch {
        return [];
    }
}

function startTimelapseCapture(webContents, sessionDir) {
    const frames = [];
    let frameIndex = 0;
    let stopped = false;
    const timer = setInterval(async () => {
        if (stopped) return;
        try {
            const image = await webContents.capturePage();
            const pngBuffer = image.toPNG();
            const fileName = `timelapse-${String(frameIndex).padStart(4, '0')}.png`;
            const targetPath = path.join(sessionDir, fileName);
            fs.writeFileSync(targetPath, pngBuffer);
            frames.push({ index: frameIndex, path: targetPath });
            frameIndex += 1;
        } catch {
            // Ignore individual capture failures.
        }
    }, TIMELAPSE_INTERVAL_MS);
    return {
        stop() {
            stopped = true;
            clearInterval(timer);
            return frames;
        },
    };
}

function composeTimelapse(sessionDir, outputPath) {
    const ffmpegBinary = findFfmpegBinary();
    if (!ffmpegBinary) {
        return { ok: false, error: 'ffmpeg not found' };
    }
    const args = [
        '-y',
        '-framerate', '4',
        '-pattern_type', 'glob',
        '-i', path.join(sessionDir, 'timelapse-*.png'),
        '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
        '-pix_fmt', 'yuv420p',
        '-preset', 'ultrafast',
        outputPath,
    ];
    const result = spawnSync(ffmpegBinary, args, { stdio: 'ignore' });
    if (result.status === 0 && fs.existsSync(outputPath)) {
        return { ok: true, path: outputPath, binary: ffmpegBinary };
    }
    return {
        ok: false,
        error: `ffmpeg timelapse exited with status ${result.status ?? 'unknown'}`,
        binary: ffmpegBinary,
    };
}

async function readRuntimeSnapshot(webContents) {
    try {
        return await webContents.executeJavaScript(`
            (() => {
                const state = window.__evermindQaState || { pageErrors: [], rejections: [] };
                const elements = Array.from(document.querySelectorAll('button, a, [role="button"], input, canvas, [tabindex]'));
                const visibleElements = elements.filter((element) => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 8 && rect.height > 8 && style.visibility !== 'hidden' && style.display !== 'none';
                });
                return {
                    title: document.title || '',
                    bodyTextLength: (document.body?.innerText || '').trim().length,
                    canvasCount: document.querySelectorAll('canvas').length,
                    visibleInteractiveCount: visibleElements.length,
                    pageErrors: state.pageErrors || [],
                    rejections: state.rejections || [],
                };
            })();
        `, true);
    } catch {
        return {
            title: '',
            bodyTextLength: 0,
            canvasCount: 0,
            visibleInteractiveCount: 0,
            pageErrors: [],
            rejections: [],
        };
    }
}

async function resolveInteractionTarget(webContents) {
    try {
        return await webContents.executeJavaScript(`
            (() => {
                const visible = (element) => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 8 && rect.height > 8 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const keywords = /(start|play|begin|launch|retry|restart|continue|tap to start|new game|开始|游玩|启动|进入|继续|重试|再来一次)/i;
                const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], [data-testid], [aria-label], canvas'));
                for (const element of candidates) {
                    if (!visible(element)) continue;
                    const text = [element.innerText, element.textContent, element.getAttribute('aria-label'), element.getAttribute('data-testid')]
                        .filter(Boolean)
                        .join(' ')
                        .trim();
                    if (!(keywords.test(text) || element.tagName.toLowerCase() === 'canvas')) continue;
                    const rect = element.getBoundingClientRect();
                    const selector = element.id ? ('#' + element.id) : '';
                    return {
                        ok: true,
                        kind: element.tagName.toLowerCase() === 'canvas' ? 'canvas' : 'element',
                        tagName: element.tagName.toLowerCase(),
                        label: text || element.tagName.toLowerCase(),
                        selector,
                        x: Math.round(rect.left + rect.width / 2),
                        y: Math.round(rect.top + rect.height / 2),
                    };
                }
                const canvas = Array.from(document.querySelectorAll('canvas'))
                    .filter((element) => visible(element))
                    .sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height) - (a.getBoundingClientRect().width * a.getBoundingClientRect().height))[0];
                if (canvas) {
                    const rect = canvas.getBoundingClientRect();
                    return {
                        ok: true,
                        kind: 'canvas',
                        tagName: 'canvas',
                        label: 'canvas',
                        selector: '',
                        x: Math.round(rect.left + rect.width / 2),
                        y: Math.round(rect.top + rect.height / 2),
                    };
                }
                return {
                    ok: true,
                    kind: 'viewport',
                    tagName: 'body',
                    label: 'viewport center',
                    selector: '',
                    x: Math.max(24, Math.round(window.innerWidth / 2)),
                    y: Math.max(24, Math.round(window.innerHeight / 2)),
                };
            })();
        `, true);
    } catch {
        return {
            ok: true,
            kind: 'viewport',
            tagName: 'body',
            label: 'viewport center',
            selector: '',
            x: 320,
            y: 240,
        };
    }
}

async function dispatchClick(webContents, target) {
    const x = Number(target?.x || 0);
    const y = Number(target?.y || 0);
    const selector = String(target?.selector || '');
    try {
        webContents.sendInputEvent({ type: 'mouseMove', x, y });
        webContents.sendInputEvent({ type: 'mouseDown', x, y, button: 'left', clickCount: 1 });
        webContents.sendInputEvent({ type: 'mouseUp', x, y, button: 'left', clickCount: 1 });
        return { ok: true, mode: 'native' };
    } catch {
        try {
            await webContents.executeJavaScript(`
                (() => {
                    const target = ${selector ? `document.querySelector(${JSON.stringify(selector)})` : 'null'}
                        || document.elementFromPoint(${x}, ${y})
                        || document.body;
                    if (!target) return false;
                    const rect = target.getBoundingClientRect();
                    const clientX = ${x};
                    const clientY = ${y};
                    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        target.dispatchEvent(new MouseEvent(type, {
                            bubbles: true,
                            cancelable: true,
                            composed: true,
                            clientX,
                            clientY,
                            button: 0,
                            view: window,
                        }));
                    }
                    if (typeof target.click === 'function') target.click();
                    return true;
                })();
            `, true);
            return { ok: true, mode: 'dom' };
        } catch (error) {
            return { ok: false, mode: 'failed', error: String(error?.message || error || 'click failed') };
        }
    }
}

async function dispatchKeySequence(webContents, keys) {
    const normalizedKeys = Array.isArray(keys) && keys.length > 0 ? keys.map((key) => String(key || '').trim()).filter(Boolean) : DEFAULT_KEY_SEQUENCE;
    let sent = 0;
    try {
        await webContents.executeJavaScript('window.focus(); document.body && document.body.focus && document.body.focus(); true;', true);
    } catch {
        // Ignore focus failures.
    }

    for (const key of normalizedKeys) {
        let sentThisKey = false;
        try {
            webContents.sendInputEvent({ type: 'keyDown', keyCode: key });
            await delay(70);
            webContents.sendInputEvent({ type: 'keyUp', keyCode: key });
            sentThisKey = true;
        } catch {
            try {
                await webContents.executeJavaScript(`
                    (() => {
                        const key = ${JSON.stringify(key)};
                        const eventInit = { key, code: key, bubbles: true, cancelable: true };
                        for (const type of ['keydown', 'keyup']) {
                            const evt = new KeyboardEvent(type, eventInit);
                            window.dispatchEvent(evt);
                            document.dispatchEvent(evt);
                            if (document.activeElement) {
                                document.activeElement.dispatchEvent(evt);
                            }
                        }
                        return true;
                    })();
                `, true);
                sentThisKey = true;
            } catch {
                sentThisKey = false;
            }
        }
        if (sentThisKey) {
            sent += 1;
        }
        await delay(110);
    }
    return { ok: sent > 0, sent, keys: normalizedKeys };
}

async function captureFrame(webContents, sessionDir, index, label) {
    const image = await webContents.capturePage();
    const pngBuffer = image.toPNG();
    const fileName = `frame-${String(index).padStart(2, '0')}-${sanitizeSegment(label, 'capture')}.png`;
    const targetPath = path.join(sessionDir, fileName);
    fs.writeFileSync(targetPath, pngBuffer);
    return {
        index,
        label,
        path: targetPath,
        stateHash: hashBuffer(pngBuffer),
    };
}

async function tryCaptureDiagnosticFrame(webContents, sessionDir, index, label) {
    try {
        return await captureFrame(webContents, sessionDir, index, label);
    } catch {
        return null;
    }
}

function findFfmpegBinary() {
    const candidates = [
        process.env.FFMPEG_PATH,
        '/opt/homebrew/bin/ffmpeg',
        '/usr/local/bin/ffmpeg',
        'ffmpeg',
    ].filter(Boolean);
    for (const candidate of candidates) {
        if (candidate === 'ffmpeg') return candidate;
        try {
            if (fs.existsSync(candidate)) return candidate;
        } catch {
            // Ignore bad candidate paths.
        }
    }
    return null;
}

function composeVideo(frameGlob, outputPath) {
    const ffmpegBinary = findFfmpegBinary();
    if (!ffmpegBinary) {
        return { ok: false, error: 'ffmpeg not found' };
    }
    const args = [
        '-y',
        '-framerate', '2',
        '-pattern_type', 'glob',
        '-i', frameGlob,
        '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
        '-pix_fmt', 'yuv420p',
        outputPath,
    ];
    const result = spawnSync(ffmpegBinary, args, { stdio: 'ignore' });
    if (result.status === 0 && fs.existsSync(outputPath)) {
        return { ok: true, path: outputPath, binary: ffmpegBinary };
    }
    return {
        ok: false,
        error: `ffmpeg exited with status ${result.status ?? 'unknown'}`,
        binary: ffmpegBinary,
    };
}

async function runQaSession(config = {}, getParentWindow) {
    const previewUrl = String(config.previewUrl || config.preview_url || '').trim();
    if (!previewUrl) {
        throw new Error('previewUrl is required');
    }

    const sessionId = sanitizeSegment(
        config.sessionId || config.session_id || `qa-${Date.now()}`,
        `qa-${Date.now()}`,
    );
    const qaRoot = ensureDir(path.join(app.getPath('userData'), 'qa-sessions'));
    const sessionDir = ensureDir(path.join(qaRoot, sessionId));
    const logPath = path.join(sessionDir, 'session-log.json');
    const width = Math.max(640, Number(config.width || DEFAULT_WIDTH));
    const height = Math.max(480, Number(config.height || DEFAULT_HEIGHT));
    const settleMs = Math.max(200, Number(config.settleMs || config.settle_ms || DEFAULT_SETTLE_MS));
    const keepOpenMs = Math.max(0, Number(config.keepOpenMs || config.keep_open_ms || DEFAULT_KEEP_OPEN_MS));
    const keySequence = Array.isArray(config.keySequence || config.key_sequence)
        ? (config.keySequence || config.key_sequence)
        : DEFAULT_KEY_SEQUENCE;

    const result = {
        ok: false,
        status: 'starting',
        sessionId,
        sessionDir,
        previewUrl,
        scenario: String(config.scenario || '').trim(),
        agent: String(config.agent || '').trim(),
        runId: String(config.runId || config.run_id || '').trim(),
        nodeExecutionId: String(config.nodeExecutionId || config.node_execution_id || '').trim(),
        startedAt: new Date().toISOString(),
        endedAt: '',
        frames: [],
        actions: [],
        videoPath: '',
        timelapsePath: '',
        rrwebRecordingPath: '',
        rrwebEventCount: 0,
        timelapseFrameCount: 0,
        logPath,
        consoleErrors: [],
        failedRequests: [],
        pageErrors: [],
        metrics: {
            width,
            height,
            settleMs,
            requestedDurationMs: Math.max(0, Number(config.durationMs || config.duration_ms || 0)),
            keepOpenMs,
        },
        summary: '',
        error: '',
    };

    let qaWindow = null;
    let completedListener = null;
    let errorListener = null;
    let timelapseCapture = null;

    try {
        const parentWindow = typeof getParentWindow === 'function' ? getParentWindow() || null : null;
        let initialBounds = null;
        if (parentWindow && !parentWindow.isDestroyed()) {
            try {
                initialBounds = parentWindow.getBounds();
            } catch {
                initialBounds = null;
            }
        }
        qaWindow = new BrowserWindow({
            width,
            height,
            x: initialBounds ? initialBounds.x + Math.max(32, Math.round((initialBounds.width - width) / 2)) : undefined,
            y: initialBounds ? initialBounds.y + Math.max(56, Math.round((initialBounds.height - height) / 2)) : undefined,
            show: true,
            alwaysOnTop: true,
            title: 'Evermind QA Preview Session',
            parent: parentWindow || undefined,
            backgroundColor: '#000000',
            autoHideMenuBar: true,
            paintWhenInitiallyHidden: true,
            fullscreenable: false,
            minimizable: true,
            resizable: true,
            webPreferences: {
                contextIsolation: true,
                nodeIntegration: false,
                backgroundThrottling: false,
                sandbox: true,
                autoplayPolicy: 'no-user-gesture-required',
                partition: `qa:${sessionId}`,
            },
        });

        const { webContents } = qaWindow;
        webContents.setAudioMuted(true);
        qaWindow.setMenuBarVisibility(false);
        qaWindow.show();
        qaWindow.focus();
        qaWindow.moveTop();

        webContents.on('console-message', (_event, level, message, line, sourceId) => {
            if (level >= 2) {
                result.consoleErrors.push({
                    level,
                    message: String(message || ''),
                    line: Number(line || 0),
                    source: String(sourceId || ''),
                });
            }
        });

        completedListener = (details) => {
            if (!details || !details.url || !String(details.url).startsWith(previewUrl.split('?')[0])) return;
            if (Number(details.statusCode || 0) >= 400) {
                result.failedRequests.push({
                    url: String(details.url || ''),
                    statusCode: Number(details.statusCode || 0),
                    method: String(details.method || ''),
                });
            }
        };
        errorListener = (details) => {
            if (!details || !details.url || !String(details.url).startsWith(previewUrl.split('?')[0])) return;
            result.failedRequests.push({
                url: String(details.url || ''),
                error: String(details.error || ''),
                method: String(details.method || ''),
            });
        };
        const requestFilter = { urls: ['*://*/*'] };
        webContents.session.webRequest.onCompleted(requestFilter, completedListener);
        webContents.session.webRequest.onErrorOccurred(requestFilter, errorListener);

        result.status = 'loading';
        await webContents.loadURL(previewUrl);
        const pageReady = await waitForPageReady(webContents);
        await installPageErrorHooks(webContents);
        await installRrwebRecording(webContents);
        timelapseCapture = startTimelapseCapture(webContents, sessionDir);
        await delay(settleMs);

        const initialFrame = await captureFrame(webContents, sessionDir, 0, 'loaded');
        result.frames.push(initialFrame);
        result.actions.push({
            action: 'snapshot',
            ok: true,
            url: previewUrl,
            state_hash: initialFrame.stateHash,
            capture_path: initialFrame.path,
            console_error_count: result.consoleErrors.length,
            page_error_count: 0,
            failed_request_count: result.failedRequests.length,
            observation: pageReady?.partial
                ? `Initial preview frame captured after partial readiness (${String(pageReady.mode || 'partial')})`
                : 'Initial preview frame captured',
        });
        await delay(SHOWCASE_DELAY_MS);

        const target = await resolveInteractionTarget(webContents);
        const clickResult = await dispatchClick(webContents, target);
        await delay(settleMs);
        const clickedFrame = await captureFrame(webContents, sessionDir, 1, 'after-click');
        result.frames.push(clickedFrame);
        result.actions.push({
            action: 'click',
            ok: Boolean(clickResult.ok),
            url: previewUrl,
            target: String(target?.label || target?.tagName || 'interactive target'),
            state_hash: clickedFrame.stateHash,
            previous_state_hash: initialFrame.stateHash,
            state_changed: clickedFrame.stateHash !== initialFrame.stateHash,
            capture_path: clickedFrame.path,
            console_error_count: result.consoleErrors.length,
            page_error_count: 0,
            failed_request_count: result.failedRequests.length,
            observation: clickResult.ok ? `Clicked ${String(target?.label || target?.tagName || 'target')}` : String(clickResult.error || 'Click failed'),
        });
        await delay(SHOWCASE_DELAY_MS);

        const keyResult = await dispatchKeySequence(webContents, keySequence);
        await delay(settleMs);
        const gameplayFrame = await captureFrame(webContents, sessionDir, 2, 'after-keys');
        result.frames.push(gameplayFrame);
        result.actions.push({
            action: 'press_sequence',
            ok: Boolean(keyResult.ok),
            url: previewUrl,
            keys_count: Number(keyResult.sent || 0),
            state_hash: gameplayFrame.stateHash,
            previous_state_hash: clickedFrame.stateHash,
            state_changed: gameplayFrame.stateHash !== clickedFrame.stateHash,
            capture_path: gameplayFrame.path,
            console_error_count: result.consoleErrors.length,
            page_error_count: 0,
            failed_request_count: result.failedRequests.length,
            observation: keyResult.ok ? `Sent ${keyResult.sent} gameplay key inputs` : 'Gameplay key sequence failed',
        });
        await delay(SHOWCASE_DELAY_MS);

        const finalFrame = await captureFrame(webContents, sessionDir, 3, 'final');
        result.frames.push(finalFrame);
        result.actions.push({
            action: 'snapshot',
            ok: true,
            url: previewUrl,
            state_hash: finalFrame.stateHash,
            previous_state_hash: gameplayFrame.stateHash,
            state_changed: finalFrame.stateHash !== gameplayFrame.stateHash,
            capture_path: finalFrame.path,
            console_error_count: result.consoleErrors.length,
            page_error_count: 0,
            failed_request_count: result.failedRequests.length,
            observation: 'Final gameplay frame captured',
        });

        const runtimeSnapshot = await readRuntimeSnapshot(webContents);
        const pageErrors = []
            .concat(Array.isArray(runtimeSnapshot?.pageErrors) ? runtimeSnapshot.pageErrors : [])
            .concat(Array.isArray(runtimeSnapshot?.rejections) ? runtimeSnapshot.rejections : []);
        result.pageErrors = pageErrors.slice(0, 30);
        for (const action of result.actions) {
            action.page_error_count = result.pageErrors.length;
            action.console_error_count = result.consoleErrors.length;
            action.failed_request_count = result.failedRequests.length;
        }

        // Collect rrweb DOM recording
        const rrwebEvents = await collectRrwebEvents(webContents);
        if (rrwebEvents.length > 0) {
            const rrwebPath = path.join(sessionDir, 'rrweb-recording.json');
            try {
                fs.writeFileSync(rrwebPath, JSON.stringify(rrwebEvents), 'utf8');
                result.rrwebRecordingPath = rrwebPath;
                result.rrwebEventCount = rrwebEvents.length;
            } catch {
                // Ignore rrweb save failures.
            }
        }

        // Stop timelapse capture and compose video
        let timelapseFrames = [];
        if (timelapseCapture) {
            timelapseFrames = timelapseCapture.stop();
            result.timelapseFrameCount = timelapseFrames.length;
        }
        if (timelapseFrames.length >= 4) {
            const timelapsePath = path.join(sessionDir, 'timelapse.mp4');
            const timelapseResult = composeTimelapse(sessionDir, timelapsePath);
            if (timelapseResult.ok) {
                result.timelapsePath = timelapseResult.path;
            }
        }

        const videoOutputPath = path.join(sessionDir, 'session.mp4');
        const videoResult = composeVideo(path.join(sessionDir, 'frame-*.png'), videoOutputPath);
        if (videoResult.ok) {
            result.videoPath = videoResult.path;
        }

        const distinctStateHashes = Array.from(new Set(
            result.actions
                .map((item) => String(item.state_hash || '').trim())
                .filter(Boolean),
        ));
        result.ok = Boolean(
            clickResult.ok
            && keyResult.ok
            && Number(keyResult.sent || 0) >= 3
            && distinctStateHashes.length >= 2
        );
        result.status = result.ok ? 'completed' : 'incomplete';
        result.summary = [
            `Desktop QA session ${result.status}`,
            `frames=${result.frames.length}`,
            `keys=${Number(keyResult.sent || 0)}`,
            `states=${distinctStateHashes.length}`,
            `console_errors=${result.consoleErrors.length}`,
            `page_errors=${result.pageErrors.length}`,
            `failed_requests=${result.failedRequests.length}`,
            `rrweb_events=${result.rrwebEventCount}`,
            `timelapse_frames=${result.timelapseFrameCount}`,
        ].join(', ');
    } catch (error) {
        const diagnosticFrame = qaWindow && !qaWindow.isDestroyed()
            ? await tryCaptureDiagnosticFrame(qaWindow.webContents, sessionDir, result.frames.length, 'diagnostic')
            : null;
        if (diagnosticFrame) {
            result.frames.push(diagnosticFrame);
            result.actions.push({
                action: 'snapshot',
                ok: false,
                url: previewUrl,
                state_hash: diagnosticFrame.stateHash,
                previous_state_hash: result.actions.length > 0 ? String(result.actions[result.actions.length - 1]?.state_hash || '') : '',
                state_changed: true,
                capture_path: diagnosticFrame.path,
                console_error_count: result.consoleErrors.length,
                page_error_count: result.pageErrors.length,
                failed_request_count: result.failedRequests.length,
                observation: `Diagnostic capture after QA error: ${String(error?.message || error || 'unknown error')}`,
            });
        }
        result.ok = false;
        result.error = String(error?.stack || error?.message || error || 'QA session failed');
        if (result.frames.length > 0 || result.actions.length > 0 || result.consoleErrors.length > 0 || result.failedRequests.length > 0) {
            result.status = 'incomplete';
            result.summary = `Desktop QA session incomplete: ${String(error?.message || error || 'unknown error')}`;
        } else {
            result.status = 'failed';
            result.summary = `Desktop QA session failed: ${String(error?.message || error || 'unknown error')}`;
        }
    } finally {
        if (timelapseCapture) {
            try { timelapseCapture.stop(); } catch { /* ignore */ }
        }
        result.endedAt = new Date().toISOString();
        writeJson(logPath, result);
        if (qaWindow && !qaWindow.isDestroyed()) {
            try {
                if (keepOpenMs > 0) {
                    await delay(keepOpenMs);
                }
                qaWindow.destroy();
            } catch {
                // Ignore window cleanup failures.
            }
        }
    }

    return result;
}

function registerQaSessionHandlers({ getParentWindow } = {}) {
    try {
        ipcMain.removeHandler('evermind:qa-run-session');
    } catch {
        // Ignore missing handler cleanup.
    }
    ipcMain.handle('evermind:qa-run-session', async (_event, config) => {
        return runQaSession(config, getParentWindow);
    });
}

module.exports = {
    registerQaSessionHandlers,
    runQaSession,
};
