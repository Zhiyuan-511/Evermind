/**
 * Evermind Desktop — Preload Script
 * Exposes minimal safe APIs to the renderer.
 */
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('evermind', {
    platform: process.platform,
    isDesktop: true,
    version: '2.0.0',
});
