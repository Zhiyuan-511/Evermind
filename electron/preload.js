/**
 * Evermind Desktop — Preload Script
 * Exposes minimal safe APIs to the renderer.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('evermind', {
    platform: process.platform,
    isDesktop: true,
    version: '2.0.0',
    revealInFinder: (targetPath) => ipcRenderer.invoke('evermind:reveal-in-finder', targetPath),
    openPath: (targetPath) => ipcRenderer.invoke('evermind:open-path', targetPath),
    pickFolder: (defaultPath) => ipcRenderer.invoke('evermind:pick-folder', defaultPath),
    qa: {
        runSession: (config) => ipcRenderer.invoke('evermind:qa-run-session', config),
    },
});
