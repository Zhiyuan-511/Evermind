'use client';

import React, { memo, useState, useCallback, useRef, useEffect } from 'react';
import { Handle, Position, useReactFlow, type NodeProps } from '@xyflow/react';
import { NODE_TYPES } from '@/lib/types';
import type { CanvasNodeStatus } from '@/lib/types';
import { buildReadableNodePreview } from '@/lib/nodeOutputHumanizer';
import { normalizeRuntimeModeForDisplay } from '@/lib/runtimeDisplay';

// ── V1 Status Config ──
const STATUS_CONFIG: Record<CanvasNodeStatus, {
    color: string; glow: string; dot: string;
    label_en: string; label_zh: string;
    pulse?: boolean;
}> = {
    queued:            { color: '#8b8fa3', glow: 'none', dot: '#8b8fa3', label_en: 'Queued', label_zh: '排队中' },
    running:           { color: '#4f8fff', glow: '0 0 14px #4f8fff30', dot: '#4f8fff', label_en: 'Running', label_zh: '执行中', pulse: true },
    passed:            { color: '#40d67c', glow: '0 0 8px #40d67c20', dot: '#40d67c', label_en: 'Passed', label_zh: '已完成' },
    failed:            { color: '#ff4f6a', glow: '0 0 10px #ff4f6a25', dot: '#ff4f6a', label_en: 'Failed', label_zh: '失败' },
    blocked:           { color: '#ff9b47', glow: '0 0 8px #ff9b4720', dot: '#ff9b47', label_en: 'Blocked', label_zh: '阻塞' },
    waiting_approval:  { color: '#f59e0b', glow: '0 0 12px #f59e0b25', dot: '#f59e0b', label_en: 'Awaiting Review', label_zh: '待审核', pulse: true },
    skipped:           { color: '#666', glow: 'none', dot: '#555', label_en: 'Skipped', label_zh: '已跳过' },
    idle:              { color: '#555', glow: 'none', dot: 'var(--node-dot-idle)', label_en: '', label_zh: '' },
    done:              { color: '#40d67c', glow: '0 0 8px #40d67c20', dot: '#40d67c', label_en: 'Complete', label_zh: '已完成' },
    error:             { color: '#ff4f6a', glow: '0 0 10px #ff4f6a25', dot: '#ff4f6a', label_en: 'Error', label_zh: '错误' },
};

function getStatusConfig(status: string) {
    return STATUS_CONFIG[(status as CanvasNodeStatus)] || STATUS_CONFIG.idle;
}

function getNodeMark(label: string, fallback: string): string {
    const source = (label || fallback || 'node').trim();
    const compact = source.replace(/\s+/g, ' ').trim();
    const han = compact.match(/[\p{Script=Han}A-Za-z0-9]/gu) || [];
    if (han.length >= 2) return `${han[0]}${han[1]}`.toUpperCase();
    if (han.length === 1) return han[0].toUpperCase();
    const words = compact.split(/[\s/_-]+/).filter(Boolean);
    if (words.length >= 2) return `${words[0][0]}${words[1][0]}`.toUpperCase();
    return compact.slice(0, 2).toUpperCase();
}

// Format helpers
function formatTokens(n: number): string {
    if (!n) return '';
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
    return String(n);
}

function formatCost(n: number): string {
    if (!n) return '';
    if (n < 0.01) return `$${n.toFixed(4)}`;
    return `$${n.toFixed(2)}`;
}

function formatDuration(startMs: number, endMs: number): string {
    if (!startMs) return '';
    const end = endMs || Date.now();
    const sec = Math.round((end - startMs) / 1000);
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
    return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function formatLatency(ms: number): string {
    if (!ms) return '';
    if (ms >= 60000) return `${(ms / 60000).toFixed(1)}m`;
    if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
    return `${Math.round(ms)}ms`;
}

function getLatencyColor(ms: number): string {
    if (ms <= 10000) return '#40d67c';   // green: fast (<10s)
    if (ms <= 30000) return '#f59e0b';   // amber: moderate (<30s)
    return '#ff4f6a';                     // red: slow (>30s)
}

// v5.8.6: dynamic model list — only shows models whose provider has a key
// configured. Fetched from /api/models (which returns has_key per model) and
// cached at module level so all AgentNode instances share one fetch.
const PROVIDER_LABEL: Record<string, string> = {
    openai: 'OpenAI',
    anthropic: 'Claude',
    google: 'Gemini',
    deepseek: 'DeepSeek',
    kimi: 'Kimi',
    qwen: 'Qwen',
    zhipu: 'Zhipu/GLM',
    doubao: 'Doubao',
    yi: 'Yi',
    minimax: 'MiniMax',
    aigate: 'AiGate',
    ollama: 'Ollama',
};
interface AvailableModel { id: string; label: string; provider: string }
// Module-level cache — shared across all AgentNode instances.
let _modelCatalogCache: AvailableModel[] | null = null;
let _modelCatalogFetchedAt = 0;
const _modelCatalogSubscribers = new Set<(list: AvailableModel[]) => void>();
const MODEL_CATALOG_TTL_MS = 30_000;

async function fetchAvailableModels(): Promise<AvailableModel[]> {
    // v5.8.6 FIX: removed 30s cache — if a stale Backend response (e.g.
    // before the /api/models duplicate-route fix) ever set the cache with
    // all-models (no has_key filter applied), users saw claude/gemini/etc
    // in the dropdown even after backend started returning has_key=false
    // for those providers. Always hit /api/models on each fetch; the
    // endpoint is cheap (~5ms) and the dropdown open is rare.
    const now = Date.now();
    try {
        const base = (typeof window !== 'undefined' && (window as unknown as { __evermindApiBase?: string }).__evermindApiBase)
            || 'http://127.0.0.1:8765';
        const resp = await fetch(`${base}/api/models`, { cache: 'no-store' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: { models?: Array<{ id: string; provider: string; has_key?: boolean }> } = await resp.json();
        const models = (data.models || [])
            .filter((m) => m.has_key === true)
            .map((m) => ({
                id: m.id,
                label: m.id,
                provider: PROVIDER_LABEL[m.provider] || m.provider,
            }));
        _modelCatalogCache = models;
        _modelCatalogFetchedAt = now;
        _modelCatalogSubscribers.forEach((cb) => { try { cb(models); } catch { /* swallow */ } });
        return models;
    } catch {
        const fallback: AvailableModel[] = [
            { id: 'aigate-kimi-k2.5', label: 'aigate-kimi-k2.5', provider: 'AiGate' },
            { id: 'aigate-qwen3.6-plus', label: 'aigate-qwen3.6-plus', provider: 'AiGate' },
            { id: 'aigate-deepseek-v3.2', label: 'aigate-deepseek-v3.2', provider: 'AiGate' },
        ];
        _modelCatalogCache = fallback;
        _modelCatalogFetchedAt = now;
        return fallback;
    }
}

// Force a refresh — called by Settings save to repopulate immediately.
export function invalidateAvailableModels(): void {
    _modelCatalogCache = null;
    _modelCatalogFetchedAt = 0;
    fetchAvailableModels();  // re-prime
}

function AgentNode({ id, data, selected }: NodeProps) {
    // v7.26: merger/patcher/etc nodes were displaying as
    // "builder" when an upstream update path lost data.nodeType. Walk a
    // fallback chain (nodeKey → rawNodeKey → agent → infer-from-id) before
    // giving up to "builder", so a transient nodeType drop on UI sync no
    // longer mislabels the canvas.
    const _idLower = (id || '').toLowerCase();
    const _idHints = ['merger', 'patcher', 'reviewer', 'analyst', 'planner', 'uidesign', 'scribe', 'polisher', 'deployer', 'debugger', 'imagegen', 'spritesheet', 'assetimport', 'tester', 'router', 'builder1', 'builder2', 'builder'];
    const _idGuess = _idHints.find((h) => _idLower.includes(h));
    // v7.39: old user templates (saved before v7.34) had
    // every node's key stored as the React Flow wrapper-type "agent" because
    // the save flow read n.type instead of n.data.nodeType. Reloading those
    // templates produced data.nodeType="agent" → NODE_TYPES["agent"] is
    // undefined → fallback default → AgentNode label silently became
    // "builder" (the final fallback). Treat "agent" as no-data and let the
    // chain continue. Also change the final fallback from "builder" to
    // "unknown" so the bug becomes VISIBLE instead of misleading.
    const _rawNodeType = (data.nodeType as string) || '';
    const _normalizedNodeType = _rawNodeType.toLowerCase() === 'agent' ? '' : _rawNodeType;
    const _rawNodeKey = ((data.nodeKey as string) || '');
    const _normalizedNodeKey = _rawNodeKey.toLowerCase() === 'agent' ? '' : _rawNodeKey;
    const nodeType = _normalizedNodeType
        || _normalizedNodeKey
        || (data.rawNodeKey as string)
        || (data.agent as string)
        || _idGuess
        || 'unknown';
    const info = NODE_TYPES[nodeType] || { icon: '', color: '#666', label_en: nodeType, label_zh: nodeType, desc_en: '', desc_zh: '', inputs: [{ id: 'in', label: 'Input' }], outputs: [{ id: 'out', label: 'Output' }] };
    const rawStatus = data.status as string || 'idle';
    const progress = Math.max(0, Math.min(100, (data.progress as number) || 0));
    const model = (data.model as string) || '';
    const assignedModel = (data.assignedModel as string) || '';
    // v7.1g: friendly label for `cli:<cli>:<model>` strings.
    // Backend now writes assigned_model = "cli:gemini:gemini-3.1-pro-preview"
    // (32 chars) for CLI-routed nodes. Raw form overflows the 8px chip and
    // looks like garbage. Map to short pretty labels:
    //   cli:gemini:gemini-3.1-pro-preview → "Gemini 3.1 Pro"
    //   cli:claude:opus  → "Claude Opus"
    //   cli:codex:gpt-5.4 → "Codex GPT-5.4"
    //   cli:gemini       → "Gemini CLI"
    function formatCliLabel(raw: string): string {
        if (!raw.startsWith('cli:')) return raw;
        const parts = raw.split(':');
        const cliName = parts[1] || '';
        const subModel = (parts[2] || '').toLowerCase();
        const cliPretty = cliName.charAt(0).toUpperCase() + cliName.slice(1);
        if (!subModel) return `${cliPretty} CLI`;
        // Gemini variants
        if (subModel.includes('gemini-3.1-pro')) return 'Gemini 3.1 Pro';
        if (subModel.includes('gemini-3.0-flash')) return 'Gemini 3.0 Flash';
        if (subModel.includes('gemini-2.5-pro')) return 'Gemini 2.5 Pro';
        if (subModel.includes('gemini-2.5-flash')) return 'Gemini 2.5 Flash';
        if (subModel.includes('gemini-2.0-flash')) return 'Gemini 2.0 Flash';
        if (subModel.startsWith('gemini-')) return `Gemini ${subModel.slice(7).split('-').slice(0,2).join(' ').replace(/\b\w/g, c => c.toUpperCase())}`;
        // Claude variants
        if (subModel === 'opus') return 'Claude Opus';
        if (subModel === 'sonnet') return 'Claude Sonnet';
        if (subModel === 'haiku') return 'Claude Haiku';
        if (subModel.includes('claude-opus-4')) return 'Claude Opus 4';
        if (subModel.includes('claude-sonnet-4')) return 'Claude Sonnet 4';
        if (subModel.includes('claude-haiku-4')) return 'Claude Haiku 4';
        // Codex / OpenAI variants
        if (subModel.startsWith('gpt-')) return `Codex ${subModel.toUpperCase()}`;
        if (subModel.startsWith('o3') || subModel.startsWith('o4')) return `Codex ${subModel}`;
        // Fallback
        return `${cliPretty} ${subModel}`;
    }
    const rawModel = assignedModel || model || 'gpt-5.4';
    const displayModel = formatCliLabel(rawModel);
    const isCliModel = rawModel.startsWith('cli:');
    const lang = (data.lang as string) || 'en';
    const lastOutput = data.lastOutput as string || '';
    const outputSummary = data.outputSummary as string || '';
    const tokensUsed = data.tokensUsed as number || 0;
    const cost = data.cost as number || 0;
    const startedAt = data.startedAt as number || 0;
    const endedAt = data.endedAt as number || 0;
    const durationText = startedAt > 0 ? formatDuration(startedAt, endedAt) : '';
    // v6.0: fall back to totalLines when codeLines is 0. Non-code nodes
    // (imagegen/analyst producing markdown/SVG/JSON) previously showed 0
    // because code_lines only counts HTML/JS/CSS/Python. That made the UI
    // look frozen at "0 lines" even though the node was writing 203 lines
    // of useful output. Show whichever is larger so the bar always reflects
    // real progress.
    // v6.1: mirror NodeDetailPopup's minified-file fallback so a 49KB one-liner
    // no longer renders as "1 line". When codeLines<=1 but we clearly have a
    // fat file (kb>=2), estimate from byte size (~80 chars/line avg).
    const rawCodeLines = data.codeLines as number || 0;
    const rawTotalLines = data.totalLines as number || data.total_lines as number || 0;
    const rawCodeKb = data.codeKb as number || 0;
    const byteSize = Math.round(rawCodeKb * 1024);
    const estimatedLines = byteSize >= 2048 ? Math.max(Math.floor(byteSize / 80), 1) : rawCodeLines;
    const codeLines = rawCodeLines > 1 ? rawCodeLines : Math.max(rawCodeLines, rawTotalLines, estimatedLines);
    // v5.8.1: surface the backend-broadcast current_action so the UI shows
    // "Reading index.html" / "Editing src/foo.ts" during tool loops instead
    // of "1 line" frozen for minutes. Falls back to a generic placeholder.
    const currentAction = String(data.current_action || '').trim();
    const codeKb = data.codeKb as number || 0;
    const codeLanguages = (data.codeLanguages as string[]) || [];
    const modelLatencyMs = data.modelLatencyMs as number || 0;
    const charsPerSec = data.charsPerSec as number || 0;
    const displayOutput = buildReadableNodePreview({
        lang: lang === 'zh' ? 'zh' : 'en',
        nodeType,
        status: rawStatus,
        phase: (data.phase as string) || '',
        taskDescription: (data.taskDescription as string) || '',
        loadedSkills: ((data.loadedSkills as string[]) || []).filter(Boolean),
        outputSummary,
        lastOutput,
        logs: Array.isArray(data.log) ? (data.log as Array<{ ts?: number; msg?: string; type?: string }>) : [],
        durationText,
    });
    const label = lang === 'zh' ? info.label_zh : info.label_en;
    const desc = lang === 'zh' ? info.desc_zh : info.desc_en;
    const name = (data.label as string) || label;
    const taskDescription = (data.taskDescription as string) || '';
    const loadedSkills = (data.loadedSkills as string[]) || [];
    const c = info.color;
    const nodeMark = getNodeMark(name, nodeType);

    // Get V1 status config
    const sc = getStatusConfig(rawStatus);
    const isRunning = rawStatus === 'running';
    const hasMetrics = tokensUsed > 0 || cost > 0 || startedAt > 0 || modelLatencyMs > 0 || charsPerSec > 0;
    const nodeRuntime = normalizeRuntimeModeForDisplay(String(data.runtime || ''));
    const isOpenClaw = nodeRuntime === 'openclaw';

    // Model selector state
    const [modelOpen, setModelOpen] = useState(false);
    const dropdownRef = useRef<HTMLDivElement>(null);
    const { setNodes } = useReactFlow();

    // v5.8.6: dynamic available-models list — only shows models whose
    // provider has a key configured (has_key=true from /api/models).
    const [availableModels, setAvailableModels] = useState<AvailableModel[]>(
        _modelCatalogCache || []
    );
    useEffect(() => {
        let cancelled = false;
        const subscriber = (list: AvailableModel[]) => { if (!cancelled) setAvailableModels(list); };
        _modelCatalogSubscribers.add(subscriber);
        fetchAvailableModels().then((list) => {
            if (!cancelled) setAvailableModels(list);
        });
        // v5.8.6: listen for settings-save dispatches so dropdowns sync
        // immediately after the user adds/removes keys (no remount needed).
        const refresh = () => {
            fetchAvailableModels().then((list) => { if (!cancelled) setAvailableModels(list); });
        };
        window.addEventListener('evermind-models-changed', refresh);
        return () => {
            cancelled = true;
            _modelCatalogSubscribers.delete(subscriber);
            window.removeEventListener('evermind-models-changed', refresh);
        };
    }, []);

    const handleModelChange = useCallback((newModel: string) => {
        setNodes(nds => nds.map(n =>
            n.id === id ? { ...n, data: { ...n.data, model: newModel } } : n
        ));
        setModelOpen(false);
    }, [id, setNodes]);

    // Close dropdown when clicking outside
    useEffect(() => {
        if (!modelOpen) return;
        const handleClickOutside = (e: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(e.target as HTMLElement)) {
                setModelOpen(false);
            }
        };
        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [modelOpen]);

    // Expandable output state
    const [outputExpanded, setOutputExpanded] = useState(false);

    // V4.3 PERF: Reduced from 1s to 5s — the 1s tick caused React re-render
    // on every running node every second, a major CPU heat source.
    const [, setTick] = useState(0);
    useEffect(() => {
        if (!isRunning || !startedAt) return;
        const timer = setInterval(() => setTick(t => t + 1), 5000);
        return () => clearInterval(timer);
    }, [isRunning, startedAt]);

    // P0-3: Track if node just mounted (for entrance animation)
    const [justMounted, setJustMounted] = useState(true);
    useEffect(() => {
        const t = setTimeout(() => setJustMounted(false), 500);
        return () => clearTimeout(t);
    }, []);

    return (
        <div className="agent-node-card" style={{
            '--node-accent': c,
            width: 220,
            borderRadius: 12,
            overflow: 'visible',
            border: `1.5px solid ${selected ? c + '60' : isRunning ? sc.color + '50' : sc.color + '25'}`,
            boxShadow: isRunning
                ? `0 0 0 1px ${sc.color}30, 0 0 16px ${sc.color}20, var(--node-shadow)`
                : selected
                    ? `0 0 0 1.5px ${c}40, var(--node-shadow)`
                    : `${sc.glow}, var(--node-shadow)`,
            transition: 'all 0.25s ease',
            fontSize: 0,
            position: 'relative',
            background: 'var(--node-bg)',
            animation: isRunning ? 'nodeRunGlow 2s ease-in-out infinite' : justMounted ? 'nodeEntrance 0.4s ease-out' : 'none',
        } as React.CSSProperties}>

            {/* ── Header ── */}
            <div style={{
                padding: '7px 10px 5px',
                display: 'flex', alignItems: 'center', gap: 6,
                background: `linear-gradient(135deg, ${c}18, transparent)`,
                borderBottom: `1px solid var(--node-divider)`,
            }}>
                <span style={{
                    width: 18,
                    height: 18,
                    borderRadius: 6,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                    fontSize: 8,
                    fontWeight: 700,
                    letterSpacing: '0.06em',
                    color: c,
                    background: `${c}18`,
                    border: `1px solid ${c}24`,
                }}>
                    {nodeMark}
                </span>
                <div style={{ flex: 1, overflow: 'hidden' }}>
                    <div style={{
                        fontSize: 11, fontWeight: 600, color: 'var(--text1)',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>{name}</div>
                    <div style={{
                        fontSize: 8,
                        color: 'var(--text3)',
                        marginTop: 1,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                    }}>
                        {desc}
                    </div>
                </div>
                {/* Status dot */}
                <span style={{
                    width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
                    background: sc.dot,
                    boxShadow: sc.pulse ? `0 0 6px ${sc.dot}` : 'none',
                    animation: sc.pulse ? 'agentPulse 1.5s infinite' : 'none',
                }} />
                {info.sec && (
                    <span style={{
                        fontSize: 7, padding: '1px 4px', borderRadius: 3, fontWeight: 700,
                        background: info.sec === 'L1' ? 'rgba(88,166,255,0.15)' : info.sec === 'L2' ? 'rgba(63,185,80,0.15)' : 'rgba(210,153,34,0.15)',
                        color: info.sec === 'L1' ? '#58a6ff' : info.sec === 'L2' ? '#3fb950' : '#d29922',
                    }}>{info.sec}</span>
                )}
            </div>

            {/* ── Body ── */}
            <div style={{ padding: '5px 0 6px' }}>
                {/* Model & Type tags */}
                <div style={{
                    padding: '0 10px', marginBottom: 3,
                    display: 'flex', gap: 3, alignItems: 'center',
                    position: 'relative',
                }} ref={dropdownRef}>
                    {/* Model selector tag */}
                    <span
                        className="model-selector-tag"
                        style={{
                            padding: '1px 5px', borderRadius: 3, fontSize: 8,
                            background: assignedModel ? 'rgba(79,143,255,0.12)' : 'var(--tag-bg)',
                            color: assignedModel ? '#4f8fff' : 'var(--tag-text)',
                            border: `1px solid ${assignedModel ? 'rgba(79,143,255,0.2)' : 'var(--tag-border)'}`,
                            cursor: 'pointer',
                            display: 'inline-flex', alignItems: 'center', gap: 2,
                            userSelect: 'none',
                            transition: 'all 0.15s ease',
                        }}
                        onClick={(e) => {
                            e.stopPropagation();
                            setModelOpen(!modelOpen);
                        }}
                        title={lang === 'zh' ? '点击切换模型' : 'Click to change model'}
                    >
                        {displayModel}
                        <span style={{ fontSize: 6, opacity: 0.7, marginLeft: 1 }}>▼</span>
                    </span>
                    {isCliModel && (
                        <span style={{
                            padding: '1px 4px', borderRadius: 3, fontSize: 7, fontWeight: 700,
                            background: 'rgba(34, 197, 94, 0.14)',
                            color: '#22c55e',
                            border: '1px solid rgba(34, 197, 94, 0.25)',
                            letterSpacing: '0.04em',
                        }} title={lang === 'zh' ? `CLI 路由：${rawModel}` : `CLI route: ${rawModel}`}>CLI</span>
                    )}
                    <span style={{
                        padding: '1px 5px', borderRadius: 3, fontSize: 8,
                        background: `${c}0c`, color: c + 'bb',
                        border: `1px solid ${c}15`,
                    }}>{nodeType}</span>
                    {isOpenClaw && (
                        <span style={{
                            padding: '1px 4px', borderRadius: 3, fontSize: 7, fontWeight: 700,
                            background: 'rgba(168, 85, 247, 0.12)',
                            color: '#a855f7',
                            border: '1px solid rgba(168, 85, 247, 0.2)',
                            letterSpacing: '0.03em',
                        }}>OC</span>
                    )}

                    {/* Model dropdown */}
                    {modelOpen && (
                        <div className="model-dropdown" style={{
                            position: 'absolute',
                            top: '100%',
                            left: 10,
                            marginTop: 4,
                            zIndex: 1000,
                            width: 190,
                            maxHeight: 240,
                            overflowY: 'auto',
                            background: 'var(--surface-strong)',
                            backdropFilter: 'blur(24px)',
                            WebkitBackdropFilter: 'blur(24px)',
                            border: '1px solid var(--glass-border)',
                            borderRadius: 8,
                            boxShadow: '0 12px 40px rgba(0,0,0,0.5)',
                            padding: '4px 0',
                        }}>
                            {availableModels.length === 0 && (
                                <div style={{
                                    padding: '8px 10px', fontSize: 9,
                                    color: 'var(--text3)', textAlign: 'center',
                                }}>
                                    {lang === 'zh' ? '无可用模型 — 请在设置里配置 API key' : 'No models available — configure API keys in Settings'}
                                </div>
                            )}
                            {availableModels.map((m) => (
                                <div
                                    key={m.id}
                                    className="model-dropdown-item"
                                    style={{
                                        padding: '5px 10px',
                                        fontSize: 9,
                                        color: m.id === displayModel ? 'var(--blue)' : 'var(--text1)',
                                        background: m.id === displayModel ? 'rgba(79,143,255,0.1)' : 'transparent',
                                        cursor: 'pointer',
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        alignItems: 'center',
                                        transition: 'all 0.1s ease',
                                    }}
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        handleModelChange(m.id);
                                    }}
                                >
                                    <span style={{ fontWeight: m.id === displayModel ? 600 : 400 }}>{m.label}</span>
                                    <span style={{
                                        fontSize: 7, color: 'var(--text3)',
                                        padding: '1px 4px', borderRadius: 3,
                                        background: 'rgba(255,255,255,0.04)',
                                    }}>{m.provider}</span>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* ── Task Description — shows what the planner assigned ── */}
                {taskDescription && (
                    <div style={{
                        padding: '3px 10px', fontSize: 8, color: 'var(--text3)',
                        lineHeight: 1.35, overflow: 'hidden', textOverflow: 'ellipsis',
                        display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' as const,
                        borderBottom: '1px solid var(--node-divider)',
                        background: 'rgba(79,143,255,0.04)',
                    }}>
                        <span style={{ color: 'var(--text4)', fontWeight: 600, fontSize: 7, marginRight: 3 }}>
                            {lang === 'zh' ? '任务' : 'Task'}:
                        </span>
                        {taskDescription.length > 80 ? taskDescription.substring(0, 80) + '…' : taskDescription}
                    </div>
                )}

                {/* ── Loaded Skills — shows which skills are active ── */}
                {loadedSkills.length > 0 && (
                    <div style={{
                        padding: '3px 10px', display: 'flex', flexWrap: 'wrap', gap: 2,
                        borderBottom: '1px solid var(--node-divider)',
                    }}>
                        <span style={{ fontSize: 7, color: 'var(--text4)', fontWeight: 600, marginRight: 2 }}>
                            {lang === 'zh' ? '技能' : 'Skills'}:
                        </span>
                        {loadedSkills.slice(0, 3).map((skill: string) => (
                            <span key={skill} style={{
                                padding: '1px 4px', borderRadius: 3, fontSize: 7,
                                background: 'rgba(168,85,247,0.1)',
                                color: '#a855f7',
                                border: '1px solid rgba(168,85,247,0.15)',
                                whiteSpace: 'nowrap',
                            }}>{skill.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()).substring(0, 22)}</span>
                        ))}
                        {loadedSkills.length > 3 && (
                            <span style={{ fontSize: 7, color: 'var(--text4)' }}>+{loadedSkills.length - 3}</span>
                        )}
                    </div>
                )}

                {/* Input Ports */}
                {info.inputs.map((port) => (
                    <div key={port.id} style={{
                        display: 'flex', alignItems: 'center',
                        padding: '4px 12px 4px 6px',
                        position: 'relative',
                        minHeight: 22,
                    }}>
                        <Handle
                            type="target"
                            position={Position.Left}
                            id={port.id}
                            style={{
                                width: 9, height: 9,
                                borderRadius: '50%',
                                border: `2px solid ${c}`,
                                background: 'var(--node-bg)',
                                left: 10,
                                transition: 'all 0.15s ease',
                            }}
                        />
                        <span style={{
                            fontSize: 9, color: 'var(--text3)', marginLeft: 6,
                            display: 'flex', alignItems: 'center', gap: 3,
                        }}>
                            <span style={{ width: 3, height: 3, borderRadius: '50%', background: c, display: 'inline-block' }} />
                            {port.label}
                        </span>
                    </div>
                ))}

                {/* Output Ports */}
                {info.outputs.map((port) => (
                    <div key={port.id} style={{
                        display: 'flex', alignItems: 'center', justifyContent: 'flex-end',
                        padding: '4px 6px 4px 12px',
                        position: 'relative',
                        minHeight: 22,
                    }}>
                        <span style={{
                            fontSize: 9, color: 'var(--text3)', marginRight: 6,
                            display: 'flex', alignItems: 'center', gap: 3,
                        }}>
                            {port.label}
                            <span style={{ width: 3, height: 3, borderRadius: '50%', background: c, display: 'inline-block' }} />
                        </span>
                        <Handle
                            type="source"
                            position={Position.Right}
                            id={port.id}
                            style={{
                                width: 9, height: 9,
                                borderRadius: '50%',
                                border: `2px solid ${c}`,
                                background: 'var(--node-bg)',
                                right: 10,
                                transition: 'all 0.15s ease',
                            }}
                        />
                    </div>
                ))}

                {/* ── Progress Bar — shows whenever node is active ── */}
                {rawStatus !== 'idle' && (
                    <div style={{
                        margin: '4px 10px 0', borderRadius: 2,
                        overflow: 'hidden',
                    }}>
                        <div style={{
                            height: 3, borderRadius: 2,
                            background: 'var(--node-divider)',
                            position: 'relative',
                        }}>
                            <div style={{
                                height: '100%', borderRadius: 2, width: `${Math.min(Math.max(progress, isRunning ? 5 : 0), 100)}%`,
                                background: isRunning
                                    ? `linear-gradient(90deg, ${sc.color}, #a855f7)`
                                    : rawStatus === 'passed' || rawStatus === 'done'
                                        ? 'var(--green, #40d67c)'
                                        : `linear-gradient(90deg, ${sc.color}, ${sc.color}80)`,
                                transition: 'width 0.5s ease-out',
                                animation: isRunning ? 'progressShimmer 1.5s ease-in-out infinite' : 'none',
                            }} />
                        </div>
                        {(isRunning || progress > 0) && (
                            <div style={{
                                fontSize: 7, color: sc.color, textAlign: 'right',
                                marginTop: 1, fontWeight: 600, opacity: 0.8,
                            }}>
                                {Math.round(progress)}%
                            </div>
                        )}
                    </div>
                )}

                {/* ── Execution Metrics Footer ── */}
                {(hasMetrics || rawStatus !== 'idle') && rawStatus !== 'idle' && (
                    <div style={{
                        margin: '4px 10px 2px',
                        padding: '4px 6px',
                        borderRadius: 5,
                        background: `${sc.color}08`,
                        border: `1px solid ${sc.color}12`,
                        display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center',
                    }}>
                        {/* Duration */}
                        {startedAt > 0 && (
                            <span style={{
                                fontSize: 7, color: 'var(--text3)',
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                            }}>
                                {lang === 'zh' ? '耗时' : 'Time'} {durationText}
                            </span>
                        )}

                        {/* Tokens */}
                        {tokensUsed > 0 && (
                            <span style={{
                                fontSize: 7, color: 'var(--text3)',
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                            }}>
                                Tokens {formatTokens(tokensUsed)}
                            </span>
                        )}

                        {/* Cost */}
                        {cost > 0 && (
                            <span style={{
                                fontSize: 7, color: 'var(--text3)',
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                            }}>
                                {lang === 'zh' ? '费用' : 'Cost'} {formatCost(cost)}
                            </span>
                        )}

                        {/* Model Latency */}
                        {modelLatencyMs > 0 && (
                            <span style={{
                                fontSize: 7, color: getLatencyColor(modelLatencyMs),
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                                padding: '1px 4px', borderRadius: 3,
                                background: `${getLatencyColor(modelLatencyMs)}10`,
                                border: `1px solid ${getLatencyColor(modelLatencyMs)}20`,
                            }}>
                                {lang === 'zh' ? '延迟' : 'Latency'} {formatLatency(modelLatencyMs)}
                            </span>
                        )}

                        {/* Speed (chars/sec) */}
                        {charsPerSec > 0 && (
                            <span style={{
                                fontSize: 7, color: '#22d3ee',
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                                padding: '1px 4px', borderRadius: 3,
                                background: 'rgba(34,211,238,0.08)',
                                border: '1px solid rgba(34,211,238,0.15)',
                            }}>
                                {lang === 'zh' ? '速率' : 'Speed'} {charsPerSec.toFixed(0)} c/s
                            </span>
                        )}

                        {/* Code Lines */}
                        {codeLines > 0 && (
                            <span style={{
                                fontSize: 7, color: '#a78bfa',
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                                padding: '1px 4px', borderRadius: 3,
                                background: 'rgba(167,139,250,0.08)',
                                border: '1px solid rgba(167,139,250,0.15)',
                            }}>
                                📝 {codeLines.toLocaleString()} {lang === 'zh' ? '行' : 'lines'} / {codeKb}KB
                                {codeLanguages.length > 0 && (
                                    <span style={{ fontSize: 6, opacity: 0.75 }}>
                                        ({codeLanguages.slice(0, 3).join('+')})
                                    </span>
                                )}
                            </span>
                        )}
                        {/* v5.8.1: live activity readout.
                            - Prefer backend-broadcast current_action ("Reading index.html", "Writing game.js")
                            - Fall back to the generic "exploring" placeholder if no action yet
                            - Shown for all running nodes, not just code producers, so users always
                              see the agent is alive instead of a frozen LOC counter. */}
                        {isRunning && (currentAction || codeLines === 0) && (
                            <span
                                title={currentAction || ''}
                                style={{
                                    fontSize: 7, color: currentAction ? '#7dd3fc' : '#64748b',
                                    display: 'inline-flex', alignItems: 'center', gap: 3,
                                    padding: '1px 4px', borderRadius: 3,
                                    background: currentAction ? 'rgba(125,211,252,0.08)' : 'rgba(100,116,139,0.08)',
                                    border: `1px solid ${currentAction ? 'rgba(125,211,252,0.18)' : 'rgba(100,116,139,0.15)'}`,
                                    fontStyle: currentAction ? 'normal' : 'italic',
                                    maxWidth: 180,
                                    overflow: 'hidden',
                                    textOverflow: 'ellipsis',
                                    whiteSpace: 'nowrap',
                                }}
                            >
                                {currentAction
                                    ? (currentAction.length > 50 ? currentAction.slice(0, 50) + '…' : currentAction)
                                    : (lang === 'zh' ? '🔧 探索/规划中...' : '🔧 Exploring…')}
                            </span>
                        )}

                        {/* Spacer */}
                        <span style={{ flex: 1 }} />

                        {/* Status label */}
                        <span style={{
                            fontSize: 7, fontWeight: 600,
                            color: sc.color,
                            display: 'inline-flex', alignItems: 'center',
                            padding: '1px 5px',
                            borderRadius: 999,
                            background: `${sc.color}12`,
                            border: `1px solid ${sc.color}18`,
                        }}>
                            {lang === 'zh' ? sc.label_zh : sc.label_en}
                        </span>
                    </div>
                )}

                {/* ── Output Preview — click to expand ── */}
                {displayOutput && (
                    <div
                        style={{
                            margin: '3px 10px 0', fontSize: 8, color: 'var(--text3)', lineHeight: 1.3,
                            cursor: 'pointer',
                            ...(outputExpanded ? {
                                maxHeight: 120, overflowY: 'auto' as const, whiteSpace: 'pre-wrap' as const,
                                wordBreak: 'break-word' as const,
                                background: 'rgba(255,255,255,0.04)', borderRadius: 4, padding: '4px 6px',
                                border: '1px solid var(--node-divider)',
                            } : {
                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' as const,
                            }),
                        }}
                        onClick={(e) => { e.stopPropagation(); setOutputExpanded(!outputExpanded); }}
                        title={lang === 'zh' ? '点击展开/收起' : 'Click to expand/collapse'}
                    >
                        {outputExpanded ? displayOutput.substring(0, 600) : `${displayOutput.substring(0, 72)}${displayOutput.length > 72 ? '...' : ''}`}
                    </div>
                )}
            </div>

            <style>{`
                @keyframes agentPulse { 0%,100% { opacity:1 } 50% { opacity:0.3 } }
                @keyframes nodeRunGlow {
                    0%, 100% { box-shadow: 0 0 0 1px rgba(79,143,255,0.18), 0 0 12px rgba(79,143,255,0.12), var(--node-shadow); }
                    50% { box-shadow: 0 0 0 1.5px rgba(79,143,255,0.32), 0 0 20px rgba(79,143,255,0.22), var(--node-shadow); }
                }
                @keyframes nodeEntrance {
                    0% { opacity: 0; transform: scale(0.85); }
                    60% { opacity: 1; transform: scale(1.03); }
                    100% { opacity: 1; transform: scale(1); }
                }
                @keyframes progressShimmer {
                    0% { opacity: 0.85; }
                    50% { opacity: 1; filter: brightness(1.3); }
                    100% { opacity: 0.85; }
                }
            `}</style>
        </div>
    );
}

export default memo(AgentNode);
