'use client';

/**
 * Monaco Editor Environment Configuration
 * 
 * Ensures Monaco's web workers load correctly in BOTH browser and Electron Desktop.
 * 
 * Problem: @monaco-editor/react loads Monaco from CDN (cdn.jsdelivr.net) by default.
 * In Electron's packaged app, CDN access may fail due to:
 *   1. Network restrictions (e.g., slow CDN in China)
 *   2. Worker blob: URL creation issues in Electron's renderer
 *   3. CSP headers blocking external scripts
 *
 * Solution: Configure MonacoEnvironment.getWorker to create inline workers that
 * use importScripts() from CDN. This works because importScripts() in workers
 * is not subject to same-origin restrictions. Also explicitly configure the
 * loader paths for reliability.
 */

import { loader } from '@monaco-editor/react';

// Match the installed monaco-editor version
const MONACO_VERSION = '0.55.1';
const MONACO_VS_PATH = `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`;

// Fallback CDN mirrors (useful in regions where jsdelivr is slow)
const MONACO_VS_MIRRORS = [
    `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`,
    `https://unpkg.com/monaco-editor@${MONACO_VERSION}/min/vs`,
    `https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/${MONACO_VERSION}/min/vs`,
];

// Configure @monaco-editor/react loader with explicit CDN path
loader.config({
    paths: { vs: MONACO_VS_PATH },
});

// Set up MonacoEnvironment for reliable worker creation in Electron
if (typeof window !== 'undefined') {
    const _global = window as unknown as Record<string, unknown>;

    // Only set if not already configured
    if (!_global.MonacoEnvironment) {
        _global.MonacoEnvironment = {
            getWorker: function (_workerId: string, label: string) {
                // Determine the correct worker module path based on language
                let workerModule = 'editor/editor.worker.js';

                if (label === 'json') {
                    workerModule = 'language/json/json.worker.js';
                } else if (label === 'css' || label === 'scss' || label === 'less') {
                    workerModule = 'language/css/css.worker.js';
                } else if (label === 'html' || label === 'handlebars' || label === 'razor') {
                    workerModule = 'language/html/html.worker.js';
                } else if (label === 'typescript' || label === 'javascript') {
                    workerModule = 'language/typescript/ts.worker.js';
                }

                // Create a worker using a blob URL that imports from CDN
                // This avoids same-origin issues with direct CDN worker URLs
                // importScripts() inside workers is NOT subject to same-origin policy
                const workerCode = `
                    self.MonacoEnvironment = { baseUrl: '${MONACO_VS_PATH}/' };
                    try {
                        importScripts('${MONACO_VS_PATH}/${workerModule}');
                    } catch (e) {
                        // Try fallback mirrors
                        ${MONACO_VS_MIRRORS.slice(1).map(mirror =>
                            `try { importScripts('${mirror}/${workerModule}'); } catch (_e) {}`
                        ).join('\n                        ')}
                        console.warn('[Monaco] Worker failed to load from all CDN mirrors:', e);
                    }
                `;

                const blob = new Blob([workerCode], { type: 'text/javascript' });
                return new Worker(URL.createObjectURL(blob));
            },
        };
    }
}

export {};
