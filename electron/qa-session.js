const { app, BrowserWindow, ipcMain } = require('electron');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const DEFAULT_WIDTH = 1280;
const DEFAULT_HEIGHT = 720;
const DEFAULT_SETTLE_MS = 1200;
const DEFAULT_TIMEOUT_MS = 28000;
const DEFAULT_KEEP_OPEN_MS = 12000;
const SHOWCASE_DELAY_MS = 800;
const TIMELAPSE_INTERVAL_MS = 500;
const RRWEB_CDN_URL = 'https://cdn.jsdelivr.net/npm/rrweb@2.0.0-alpha.17/dist/rrweb.umd.cjs.js';
const DEFAULT_KEY_SEQUENCE = ['w', 'd', 'Space', 'a', 's', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'ArrowDown'];
const DEFAULT_DRAG_DISTANCE_X = 140;
const DEFAULT_DRAG_DISTANCE_Y = -32;
const DEFAULT_DRAG_STEPS = 7;
const DEFAULT_FIRE_HOLD_MS = 320;
const START_CONTROL_KEYWORDS = /(start|play|begin|launch|retry|restart|continue|tap to start|new game|开始|游玩|启动|进入|继续|重试|再来一次)/i;
const START_CONTROL_REGEX_SOURCE = START_CONTROL_KEYWORDS.source;
const START_CONTROL_REGEX_FLAGS = START_CONTROL_KEYWORDS.flags;

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

function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
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
                const keywords = new RegExp(
                    ${JSON.stringify(START_CONTROL_REGEX_SOURCE)},
                    ${JSON.stringify(START_CONTROL_REGEX_FLAGS)},
                );
                const textFor = (element) => [element.innerText, element.textContent, element.getAttribute('aria-label'), element.getAttribute('data-testid')]
                    .filter(Boolean)
                    .join(' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const buildTarget = (element, kindOverride = '') => {
                    const rect = element.getBoundingClientRect();
                    const selector = element.id ? ('#' + element.id) : '';
                    const tagName = element.tagName.toLowerCase();
                    return {
                        ok: true,
                        kind: kindOverride || (tagName === 'canvas' ? 'canvas' : 'element'),
                        tagName,
                        label: textFor(element) || tagName,
                        selector,
                        x: Math.round(rect.left + rect.width / 2),
                        y: Math.round(rect.top + rect.height / 2),
                    };
                };
                const controlCandidates = Array.from(document.querySelectorAll('button, a, [role="button"], [data-testid], [aria-label]'))
                    .filter((element) => visible(element));
                const keywordControl = controlCandidates.find((element) => keywords.test(textFor(element)));
                if (keywordControl) {
                    return buildTarget(keywordControl, 'start_control');
                }
                const genericControl = controlCandidates[0];
                if (genericControl) {
                    return buildTarget(genericControl, 'element');
                }
                const canvas = Array.from(document.querySelectorAll('canvas'))
                    .filter((element) => visible(element))
                    .sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height) - (a.getBoundingClientRect().width * a.getBoundingClientRect().height))[0];
                if (canvas) {
                    return buildTarget(canvas, 'canvas');
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

async function resolveGameplaySurface(webContents) {
    try {
        return await webContents.executeJavaScript(`
            (() => {
                const visible = (element) => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 8 && rect.height > 8 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const canvas = Array.from(document.querySelectorAll('canvas'))
                    .filter((element) => visible(element))
                    .sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height) - (a.getBoundingClientRect().width * a.getBoundingClientRect().height))[0];
                if (!canvas) return null;
                const rect = canvas.getBoundingClientRect();
                return {
                    ok: true,
                    kind: 'canvas',
                    tagName: 'canvas',
                    label: 'canvas gameplay surface',
                    selector: canvas.id ? ('#' + canvas.id) : '',
                    x: Math.round(rect.left + rect.width / 2),
                    y: Math.round(rect.top + rect.height / 2),
                };
            })();
        `, true);
    } catch {
        return null;
    }
}

async function dispatchClick(webContents, target) {
    const x = Number(target?.x || 0);
    const y = Number(target?.y || 0);
    const selector = String(target?.selector || '');
    const targetKind = String(target?.kind || '').trim().toLowerCase();
    const domClickScript = `
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
            if (typeof target.focus === 'function') target.focus();
            return {
                ok: true,
                tagName: String(target.tagName || '').toLowerCase(),
                width: Math.round(rect.width || 0),
                height: Math.round(rect.height || 0),
            };
        })();
    `;
    let nativeOk = false;
    try {
        webContents.sendInputEvent({ type: 'mouseMove', x, y });
        webContents.sendInputEvent({ type: 'mouseDown', x, y, button: 'left', clickCount: 1 });
        webContents.sendInputEvent({ type: 'mouseUp', x, y, button: 'left', clickCount: 1 });
        nativeOk = true;
    } catch {
        nativeOk = false;
    }
    if (!nativeOk || targetKind === 'start_control') {
        try {
            const domResult = await webContents.executeJavaScript(domClickScript, true);
            if (nativeOk) {
                return { ok: true, mode: 'native+dom', dom_target: domResult || {} };
            }
            return {
                ok: Boolean(domResult && domResult.ok !== false),
                mode: 'dom',
                dom_target: domResult || {},
            };
        } catch (error) {
            if (nativeOk) {
                return { ok: true, mode: 'native' };
            }
            return { ok: false, mode: 'failed', error: String(error?.message || error || 'click failed') };
        }
    }
    return { ok: true, mode: 'native' };
}

function normalizeKeyEntry(entry) {
    if (entry && typeof entry === 'object') {
        const rawKey = String(entry.key || entry.keyCode || '').trim();
        const key = rawKey === 'Space' ? ' ' : rawKey;
        const code = String(entry.code || '').trim();
        const nativeKeyCode = String(entry.nativeKeyCode || entry.keyCode || '').trim();
        const holdMs = Math.max(20, Number(entry.holdMs || entry.hold_ms || 70));
        return {
            key: key || ' ',
            code: code || inferCodeFromKey(key || nativeKeyCode || ' '),
            nativeKeyCode: nativeKeyCode || inferNativeKeyCode(key || rawKey || ' '),
            holdMs,
        };
    }
    const rawKey = String(entry || '').trim() || ' ';
    const key = rawKey === 'Space' ? ' ' : rawKey;
    return {
        key,
        code: inferCodeFromKey(key),
        nativeKeyCode: inferNativeKeyCode(key),
        holdMs: 70,
    };
}

function inferCodeFromKey(key) {
    const normalized = String(key || '').trim();
    if (normalized === ' ') return 'Space';
    if (/^Arrow/.test(normalized)) return normalized;
    if (/^[a-zA-Z]$/.test(normalized)) return `Key${normalized.toUpperCase()}`;
    return normalized || 'Space';
}

function inferNativeKeyCode(key) {
    const normalized = String(key || '').trim();
    if (normalized === ' ') return 'Space';
    if (/^Arrow/.test(normalized)) return normalized;
    if (/^[a-zA-Z]$/.test(normalized)) return normalized.toUpperCase();
    return normalized || 'Space';
}

async function dispatchKeySequence(webContents, keys) {
    const normalizedKeys = Array.isArray(keys) && keys.length > 0 ? keys.map(normalizeKeyEntry) : DEFAULT_KEY_SEQUENCE.map(normalizeKeyEntry);
    let sent = 0;
    try {
        await webContents.executeJavaScript('window.focus(); document.body && document.body.focus && document.body.focus(); true;', true);
    } catch {
        // Ignore focus failures.
    }

    for (const entry of normalizedKeys) {
        const key = String(entry.key || '').trim() || ' ';
        const code = String(entry.code || inferCodeFromKey(key)).trim();
        const nativeKeyCode = String(entry.nativeKeyCode || inferNativeKeyCode(key)).trim();
        const holdMs = Math.max(20, Number(entry.holdMs || 70));
        let sentThisKey = false;
        try {
            webContents.sendInputEvent({ type: 'keyDown', keyCode: nativeKeyCode });
            await delay(holdMs);
            webContents.sendInputEvent({ type: 'keyUp', keyCode: nativeKeyCode });
            sentThisKey = true;
        } catch {
            try {
                await webContents.executeJavaScript(`
                    (() => {
                        const key = ${JSON.stringify(key)};
                        const code = ${JSON.stringify(code)};
                        const eventInit = { key, code, bubbles: true, cancelable: true };
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
    return {
        ok: sent > 0,
        sent,
        keys: normalizedKeys.map((entry) => String(entry.code || entry.key || '').trim()).filter(Boolean),
    };
}

async function dispatchMouseDrag(webContents, target, options = {}) {
    const startX = Number(target?.x || 320);
    const startY = Number(target?.y || 240);
    const deltaX = Number(options.deltaX || DEFAULT_DRAG_DISTANCE_X);
    const deltaY = Number(options.deltaY || DEFAULT_DRAG_DISTANCE_Y);
    const steps = Math.max(3, Number(options.steps || DEFAULT_DRAG_STEPS));
    const endX = clamp(Math.round(startX + deltaX), 24, 2200);
    const endY = clamp(Math.round(startY + deltaY), 24, 1400);
    const selector = String(target?.selector || '');
    let nativeOk = false;
    try {
        webContents.sendInputEvent({ type: 'mouseMove', x: startX, y: startY });
        webContents.sendInputEvent({ type: 'mouseDown', x: startX, y: startY, button: 'left', clickCount: 1 });
        for (let index = 1; index <= steps; index += 1) {
            const progress = index / steps;
            const x = Math.round(startX + ((endX - startX) * progress));
            const y = Math.round(startY + ((endY - startY) * progress));
            webContents.sendInputEvent({ type: 'mouseMove', x, y, button: 'left', buttons: ['left'] });
            await delay(18);
        }
        webContents.sendInputEvent({ type: 'mouseUp', x: endX, y: endY, button: 'left', clickCount: 1 });
        nativeOk = true;
    } catch {
        nativeOk = false;
    }

    const domScript = `
        (() => {
            const selector = ${JSON.stringify(selector)};
            const startX = ${startX};
            const startY = ${startY};
            const endX = ${endX};
            const endY = ${endY};
            const steps = ${steps};
            const target = (selector && document.querySelector(selector))
                || document.elementFromPoint(startX, startY)
                || document.body;
            if (!target) return { ok: false };
            const emit = (type, x, y, buttons = 1) => {
                const receiver = document.elementFromPoint(x, y) || target;
                if (!receiver) return;
                receiver.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    composed: true,
                    clientX: x,
                    clientY: y,
                    button: 0,
                    buttons,
                    view: window,
                }));
            };
            emit('pointerdown', startX, startY, 1);
            emit('mousedown', startX, startY, 1);
            for (let index = 1; index <= steps; index += 1) {
                const progress = index / steps;
                const x = Math.round(startX + ((endX - startX) * progress));
                const y = Math.round(startY + ((endY - startY) * progress));
                emit('pointermove', x, y, 1);
                emit('mousemove', x, y, 1);
            }
            emit('pointerup', endX, endY, 0);
            emit('mouseup', endX, endY, 0);
            return { ok: true };
        })();
    `;
    if (!nativeOk || String(target?.kind || '').trim().toLowerCase() === 'canvas') {
        try {
            const domResult = await webContents.executeJavaScript(domScript, true);
            return {
                ok: Boolean(nativeOk || (domResult && domResult.ok)),
                mode: nativeOk ? 'native+dom' : 'dom',
                startX,
                startY,
                endX,
                endY,
                dragDistance: Math.round(Math.hypot(endX - startX, endY - startY)),
            };
        } catch (error) {
            if (nativeOk) {
                return {
                    ok: true,
                    mode: 'native',
                    startX,
                    startY,
                    endX,
                    endY,
                    dragDistance: Math.round(Math.hypot(endX - startX, endY - startY)),
                };
            }
            return { ok: false, mode: 'failed', error: String(error?.message || error || 'drag failed') };
        }
    }
    return {
        ok: true,
        mode: 'native',
        startX,
        startY,
        endX,
        endY,
        dragDistance: Math.round(Math.hypot(endX - startX, endY - startY)),
    };
}

async function dispatchMouseHold(webContents, target, holdMs = DEFAULT_FIRE_HOLD_MS) {
    const x = Number(target?.x || 320);
    const y = Number(target?.y || 240);
    const selector = String(target?.selector || '');
    let nativeOk = false;
    try {
        webContents.sendInputEvent({ type: 'mouseMove', x, y });
        webContents.sendInputEvent({ type: 'mouseDown', x, y, button: 'left', clickCount: 1 });
        await delay(Math.max(120, Number(holdMs || DEFAULT_FIRE_HOLD_MS)));
        webContents.sendInputEvent({ type: 'mouseUp', x, y, button: 'left', clickCount: 1 });
        nativeOk = true;
    } catch {
        nativeOk = false;
    }
    const domScript = `
        (() => {
            const target = ${selector ? `document.querySelector(${JSON.stringify(selector)})` : 'null'}
                || document.elementFromPoint(${x}, ${y})
                || document.body;
            if (!target) return { ok: false };
            const emit = (type, buttons = 1) => {
                target.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    composed: true,
                    clientX: ${x},
                    clientY: ${y},
                    button: 0,
                    buttons,
                    view: window,
                }));
            };
            emit('pointerdown', 1);
            emit('mousedown', 1);
            emit('pointerup', 0);
            emit('mouseup', 0);
            return { ok: true };
        })();
    `;
    if (!nativeOk || String(target?.kind || '').trim().toLowerCase() === 'canvas') {
        try {
            const domResult = await webContents.executeJavaScript(domScript, true);
            return { ok: Boolean(nativeOk || (domResult && domResult.ok)), mode: nativeOk ? 'native+dom' : 'dom', holdMs: Math.max(120, Number(holdMs || DEFAULT_FIRE_HOLD_MS)) };
        } catch (error) {
            if (nativeOk) {
                return { ok: true, mode: 'native', holdMs: Math.max(120, Number(holdMs || DEFAULT_FIRE_HOLD_MS)) };
            }
            return { ok: false, mode: 'failed', error: String(error?.message || error || 'hold fire failed') };
        }
    }
    return { ok: true, mode: 'native', holdMs: Math.max(120, Number(holdMs || DEFAULT_FIRE_HOLD_MS)) };
}

async function readGameplaySignals(webContents) {
    try {
        return await webContents.executeJavaScript(`
            (() => {
                const visible = (element) => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 8 && rect.height > 8 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textFor = (element) => [element.innerText, element.textContent, element.getAttribute('aria-label'), element.getAttribute('title')]
                    .filter(Boolean)
                    .join(' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const startKeywords = new RegExp(
                    ${JSON.stringify(START_CONTROL_REGEX_SOURCE)},
                    ${JSON.stringify(START_CONTROL_REGEX_FLAGS)},
                );
                const controls = Array.from(document.querySelectorAll('button, a, [role="button"], [data-testid], [aria-label]'))
                    .filter((element) => visible(element));
                const startControls = controls
                    .map((element) => ({ element, text: textFor(element) }))
                    .filter((entry) => startKeywords.test(entry.text));
                const overlayCandidates = Array.from(document.querySelectorAll('.overlay, .modal, dialog, [role="dialog"], [aria-modal="true"]'))
                    .filter((element) => visible(element));
                const scoreAnchors = Array.from(document.querySelectorAll('[id], [class]'))
                    .filter((element) => visible(element))
                    .filter((element) => /score|level|lives|life|health|combo|stage|hud|target/i.test(String(element.id || '') + ' ' + String(element.className || '')))
                    .slice(0, 16);
                const scoreText = scoreAnchors.map((element) => textFor(element)).filter(Boolean).slice(0, 8);
                return {
                    visibleStartCount: startControls.length,
                    visibleStartLabels: startControls.map((entry) => entry.text).filter(Boolean).slice(0, 6),
                    overlayVisible: overlayCandidates.length > 0,
                    overlayTexts: overlayCandidates.map((element) => textFor(element)).filter(Boolean).slice(0, 4),
                    scoreText,
                    scoreDigest: scoreText.join(' | ').slice(0, 400),
                    focusedTag: String(document.activeElement?.tagName || '').toLowerCase(),
                    focusedLabel: textFor(document.activeElement || document.body).slice(0, 120),
                    canvasCount: document.querySelectorAll('canvas').length,
                    bodyTextLength: (document.body?.innerText || '').trim().length,
                };
            })();
        `, true);
    } catch {
        return {
            visibleStartCount: 0,
            visibleStartLabels: [],
            overlayVisible: false,
            overlayTexts: [],
            scoreText: [],
            scoreDigest: '',
            focusedTag: '',
            focusedLabel: '',
            canvasCount: 0,
            bodyTextLength: 0,
        };
    }
}

function hasMeaningfulGameplayTransition(beforeSignals = {}, afterSignals = {}) {
    const beforeStartCount = Number(beforeSignals.visibleStartCount || 0);
    const afterStartCount = Number(afterSignals.visibleStartCount || 0);
    const beforeOverlayVisible = Boolean(beforeSignals.overlayVisible);
    const afterOverlayVisible = Boolean(afterSignals.overlayVisible);
    const beforeScoreDigest = String(beforeSignals.scoreDigest || '').trim();
    const afterScoreDigest = String(afterSignals.scoreDigest || '').trim();
    const beforeBodyLen = Number(beforeSignals.bodyTextLength || 0);
    const afterBodyLen = Number(afterSignals.bodyTextLength || 0);
    if (beforeStartCount > 0 && afterStartCount < beforeStartCount) {
        return true;
    }
    if (beforeOverlayVisible && !afterOverlayVisible) {
        return true;
    }
    if (beforeScoreDigest && afterScoreDigest && beforeScoreDigest !== afterScoreDigest) {
        return true;
    }
    // Score HUD appeared from nothing — strong gameplay start signal
    if (!beforeScoreDigest && afterScoreDigest && afterScoreDigest.length > 4) {
        return true;
    }
    if (beforeStartCount > 0 && afterStartCount === 0 && String(afterSignals.focusedTag || '').toLowerCase() === 'canvas') {
        return true;
    }
    // Significant body text increase indicates UI state change (e.g. HUD elements rendered)
    if (beforeBodyLen > 0 && afterBodyLen > beforeBodyLen * 1.5 && (afterBodyLen - beforeBodyLen) >= 40) {
        return true;
    }
    return false;
}

function isBenignConsoleMessage(message = '') {
    const text = String(message || '').trim().toLowerCase();
    if (!text) return false;
    const benignMarkers = [
        'deprecated with r150+',
        'please use es modules or alternatives',
        'build/three.js',
        'build/three.min.js',
        '[evermind-qa]',
        'favicon.ico',
        'source map',
        'sourcemap',
        'download the react devtools',
        'permissions policy violation',
    ];
    return benignMarkers.some((marker) => text.includes(marker));
}

function isFatalConsoleMessage(message = '') {
    const text = String(message || '').trim().toLowerCase();
    if (!text) return false;
    const fatalMarkers = [
        'uncaught ',
        'typeerror',
        'referenceerror',
        'syntaxerror',
        'rangeerror',
        'cannot read properties',
        'is not defined',
        'failed to load module',
        'webgl context lost',
    ];
    return fatalMarkers.some((marker) => text.includes(marker));
}

function hasRuntimeFailures(result = {}) {
    const appConsoleErrors = Array.isArray(result.consoleErrors)
        ? result.consoleErrors.filter((entry) => {
            if (!entry || entry.qaInfra) return false;
            if (entry.blocking === false) return false;
            if (entry.blocking === true) return true;
            if (isBenignConsoleMessage(entry.message || '')) return false;
            const level = Number(entry.level || 3);
            return level >= 3 || isFatalConsoleMessage(entry.message || '');
        })
        : [];
    return Boolean(
        appConsoleErrors.length > 0
        || (Array.isArray(result.pageErrors) && result.pageErrors.length > 0)
        || (Array.isArray(result.failedRequests) && result.failedRequests.length > 0)
    );
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
                const msg = String(message || '');
                const isQaInfraMessage = msg.startsWith('[evermind-qa]');
                const blockingConsoleMessage = !isQaInfraMessage
                    && !isBenignConsoleMessage(msg)
                    && (Number(level || 0) >= 3 || isFatalConsoleMessage(msg));
                result.consoleErrors.push({
                    level,
                    message: msg,
                    line: Number(line || 0),
                    source: String(sourceId || ''),
                    qaInfra: isQaInfraMessage,
                    blocking: blockingConsoleMessage,
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

        let frameIndex = 0;
        const initialFrame = await captureFrame(webContents, sessionDir, frameIndex, 'loaded');
        frameIndex += 1;
        const initialSignals = await readGameplaySignals(webContents);
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
        const clickedFrame = await captureFrame(webContents, sessionDir, frameIndex, 'after-click');
        frameIndex += 1;
        const afterClickSignals = await readGameplaySignals(webContents);
        result.frames.push(clickedFrame);
        result.actions.push({
            action: 'click',
            ok: Boolean(clickResult.ok),
            url: previewUrl,
            target: String(target?.label || target?.tagName || 'interactive target'),
            x: Number(target?.x || 0),
            y: Number(target?.y || 0),
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

        const gameplaySurface = await resolveGameplaySurface(webContents);
        if (gameplaySurface && String(target?.kind || '') !== 'canvas') {
            await dispatchClick(webContents, gameplaySurface);
            await delay(180);
        }
        let referenceTarget = gameplaySurface || target;
        let previousFrame = clickedFrame;
        let afterDragSignals = afterClickSignals;
        let afterFireSignals = afterDragSignals;
        let dragResult = { ok: false, skipped: true };
        let fireResult = { ok: false, skipped: true };

        if (referenceTarget) {
            dragResult = await dispatchMouseDrag(webContents, referenceTarget);
            await delay(Math.max(180, Math.round(settleMs * 0.6)));
            const dragFrame = await captureFrame(webContents, sessionDir, frameIndex, 'after-drag');
            frameIndex += 1;
            afterDragSignals = await readGameplaySignals(webContents);
            result.frames.push(dragFrame);
            result.actions.push({
                action: 'drag_camera',
                ok: Boolean(dragResult.ok),
                url: previewUrl,
                target: String(referenceTarget?.label || referenceTarget?.tagName || 'gameplay surface'),
                x: Number(referenceTarget?.x || 0),
                y: Number(referenceTarget?.y || 0),
                drag_distance: Number(dragResult.dragDistance || 0),
                end_x: Number(dragResult.endX || 0),
                end_y: Number(dragResult.endY || 0),
                state_hash: dragFrame.stateHash,
                previous_state_hash: previousFrame.stateHash,
                state_changed: dragFrame.stateHash !== previousFrame.stateHash,
                capture_path: dragFrame.path,
                console_error_count: result.consoleErrors.length,
                page_error_count: 0,
                failed_request_count: result.failedRequests.length,
                observation: dragResult.ok
                    ? `Dragged gameplay camera surface by ${Number(dragResult.dragDistance || 0)}px`
                    : String(dragResult.error || 'Camera drag failed'),
            });
            previousFrame = dragFrame;
            await delay(SHOWCASE_DELAY_MS);
        }

        if (gameplaySurface || referenceTarget) {
            fireResult = await dispatchMouseHold(webContents, gameplaySurface || referenceTarget, DEFAULT_FIRE_HOLD_MS);
            await delay(Math.max(180, Math.round(settleMs * 0.6)));
            const fireFrame = await captureFrame(webContents, sessionDir, frameIndex, 'after-fire');
            frameIndex += 1;
            afterFireSignals = await readGameplaySignals(webContents);
            result.frames.push(fireFrame);
            result.actions.push({
                action: 'hold_fire',
                ok: Boolean(fireResult.ok),
                url: previewUrl,
                target: String((gameplaySurface || referenceTarget)?.label || (gameplaySurface || referenceTarget)?.tagName || 'gameplay surface'),
                x: Number((gameplaySurface || referenceTarget)?.x || 0),
                y: Number((gameplaySurface || referenceTarget)?.y || 0),
                hold_ms: Number(fireResult.holdMs || DEFAULT_FIRE_HOLD_MS),
                state_hash: fireFrame.stateHash,
                previous_state_hash: previousFrame.stateHash,
                state_changed: fireFrame.stateHash !== previousFrame.stateHash,
                capture_path: fireFrame.path,
                console_error_count: result.consoleErrors.length,
                page_error_count: 0,
                failed_request_count: result.failedRequests.length,
                observation: fireResult.ok
                    ? `Held fire / pointer input for ${Number(fireResult.holdMs || DEFAULT_FIRE_HOLD_MS)}ms`
                    : String(fireResult.error || 'Fire hold failed'),
            });
            previousFrame = fireFrame;
            await delay(SHOWCASE_DELAY_MS);
        }

        const keyResult = await dispatchKeySequence(webContents, keySequence);
        await delay(settleMs);
        const gameplayFrame = await captureFrame(webContents, sessionDir, frameIndex, 'after-keys');
        frameIndex += 1;
        const afterKeysSignals = await readGameplaySignals(webContents);
        result.frames.push(gameplayFrame);
        result.actions.push({
            action: 'press_sequence',
            ok: Boolean(keyResult.ok),
            url: previewUrl,
            keys_count: Number(keyResult.sent || 0),
            state_hash: gameplayFrame.stateHash,
            previous_state_hash: previousFrame.stateHash,
            state_changed: gameplayFrame.stateHash !== previousFrame.stateHash,
            capture_path: gameplayFrame.path,
            console_error_count: result.consoleErrors.length,
            page_error_count: 0,
            failed_request_count: result.failedRequests.length,
            observation: keyResult.ok ? `Sent ${keyResult.sent} gameplay key inputs` : 'Gameplay key sequence failed',
        });
        await delay(SHOWCASE_DELAY_MS);

        const finalFrame = await captureFrame(webContents, sessionDir, frameIndex, 'final');
        frameIndex += 1;
        const finalSignals = await readGameplaySignals(webContents);
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

        const runtimeHealthy = !hasRuntimeFailures(result);
        const distinctStateHashes = Array.from(new Set(
            result.actions
                .map((item) => String(item.state_hash || '').trim())
                .filter(Boolean),
        ));
        const stateChangeCount = result.actions.filter((item) => Boolean(item.state_changed)).length;
        const pointerInteractionOk = Boolean(clickResult.ok || dragResult.ok || fireResult.ok);
        const menuTransition = (
            hasMeaningfulGameplayTransition(initialSignals, afterClickSignals)
            || hasMeaningfulGameplayTransition(initialSignals, afterDragSignals)
            || hasMeaningfulGameplayTransition(initialSignals, afterFireSignals)
            || hasMeaningfulGameplayTransition(initialSignals, afterKeysSignals)
            || hasMeaningfulGameplayTransition(initialSignals, finalSignals)
        );
        const keyDrivenTransition = (
            hasMeaningfulGameplayTransition(afterClickSignals, afterDragSignals)
            || hasMeaningfulGameplayTransition(afterDragSignals, afterFireSignals)
            || hasMeaningfulGameplayTransition(afterClickSignals, afterFireSignals)
            || hasMeaningfulGameplayTransition(afterFireSignals, afterKeysSignals)
            || hasMeaningfulGameplayTransition(afterClickSignals, afterKeysSignals)
            || hasMeaningfulGameplayTransition(afterClickSignals, finalSignals)
            || hasMeaningfulGameplayTransition(afterKeysSignals, finalSignals)
        );
        const gameplayStarted = (
            runtimeHealthy
            && pointerInteractionOk
            && (Boolean(keyResult.ok) || Boolean(dragResult.ok) || Boolean(fireResult.ok))
            && (
                Number(keyResult.sent || 0) >= 3
                || Boolean(dragResult.ok)
                || Boolean(fireResult.ok)
            )
            && (
                menuTransition
                || stateChangeCount >= 2
                || distinctStateHashes.length >= 3
            )
            && (
                keyDrivenTransition
                || stateChangeCount >= 3
                || (
                    Boolean(keyResult.ok)
                    && Boolean((gameplayFrame.stateHash || '').trim())
                    && String(gameplayFrame.stateHash || '').trim() !== String(initialFrame.stateHash || '').trim()
                )
            )
        );
        result.gameplaySignals = {
            initial: initialSignals,
            afterClick: afterClickSignals,
            afterDrag: afterDragSignals,
            afterFire: afterFireSignals,
            afterKeys: afterKeysSignals,
            final: finalSignals,
        };
        result.gameplayStarted = Boolean(gameplayStarted);
        result.interactionTarget = {
            primary: target || null,
            surface: gameplaySurface || null,
        };

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

        result.ok = Boolean(
            pointerInteractionOk
            && (Boolean(keyResult.ok) || Boolean(dragResult.ok) || Boolean(fireResult.ok))
            && distinctStateHashes.length >= 2
            && runtimeHealthy
            && gameplayStarted
        );
        result.status = result.ok ? 'completed' : 'incomplete';
        result.summary = [
            `Desktop QA session ${result.status}`,
            `frames=${result.frames.length}`,
            `keys=${Number(keyResult.sent || 0)}`,
            `states=${distinctStateHashes.length}`,
            `gameplay_started=${result.gameplayStarted ? 1 : 0}`,
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
