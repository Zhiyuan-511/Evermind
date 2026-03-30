'use client';

import { useState, useEffect, useCallback } from 'react';
import { NODE_TYPES } from '@/lib/types';

interface SettingsModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
    onLangChange: (lang: 'en' | 'zh') => void;
    theme: 'dark' | 'light';
    onThemeChange: (theme: 'dark' | 'light') => void;
    connected: boolean;
    wsUrl: string;
    onWsUrlChange: (url: string) => void;
    wsRef?: React.MutableRefObject<WebSocket | null>;
}

interface ApiKeys {
    kimi: string;
    gemini: string;
    openai: string;
    claude: string;
    deepseek: string;
    qwen: string;
}

interface ModelCatalogItem {
    id: string;
    provider: string;
}

type TabId = 'conn' | 'perm' | 'ui' | 'quality' | 'nodes';

const TABS: { id: TabId; label_en: string; label_zh: string }[] = [
    { id: 'conn', label_en: 'Connection', label_zh: '连接' },
    { id: 'perm', label_en: 'Permissions', label_zh: '权限' },
    { id: 'quality', label_en: 'Quality', label_zh: '验收策略' },
    { id: 'nodes', label_en: 'Node Models', label_zh: '节点模型' },
    { id: 'ui', label_en: 'Interface', label_zh: '界面' },
];

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
const NODE_MODEL_PRIORITY_SLOTS = [0, 1, 2] as const;
const NODE_MODEL_CONFIG_ROLES = [
    'router',
    'planner',
    'analyst',
    'uidesign',
    'scribe',
    'builder',
    'polisher',
    'reviewer',
    'tester',
    'debugger',
    'deployer',
    'imagegen',
    'spritesheet',
    'assetimport',
] as const;
const FALLBACK_MODEL_CATALOG: ModelCatalogItem[] = [
    { id: 'gpt-5.4', provider: 'openai' },
    { id: 'gpt-4.1', provider: 'openai' },
    { id: 'gpt-4o', provider: 'openai' },
    { id: 'o3', provider: 'openai' },
    { id: 'claude-4-sonnet', provider: 'anthropic' },
    { id: 'claude-4-opus', provider: 'anthropic' },
    { id: 'claude-3.5-sonnet', provider: 'anthropic' },
    { id: 'gemini-2.5-pro', provider: 'google' },
    { id: 'gemini-2.0-flash', provider: 'google' },
    { id: 'deepseek-v3', provider: 'deepseek' },
    { id: 'deepseek-r1', provider: 'deepseek' },
    { id: 'kimi', provider: 'kimi' },
    { id: 'kimi-k2.5', provider: 'kimi' },
    { id: 'kimi-coding', provider: 'kimi' },
    { id: 'qwen-max', provider: 'qwen' },
];
const PROVIDER_LABELS: Record<string, string> = {
    openai: 'OpenAI',
    anthropic: 'Claude',
    google: 'Gemini',
    deepseek: 'DeepSeek',
    kimi: 'Kimi',
    qwen: 'Qwen',
    ollama: 'Ollama',
    relay: 'Relay',
};

// ── Security: mask API key for display ──
function maskKey(key: string): string {
    if (!key) return '';
    if (key.length <= 8) return '●'.repeat(key.length);
    return key.slice(0, 4) + '●'.repeat(Math.min(key.length - 8, 20)) + key.slice(-4);
}

// ── Security: sanitize pasted content ──
function sanitizeInput(value: string): string {
    // Strip any HTML tags, script injections, and trim whitespace
    return value.replace(/<[^>]*>/g, '').replace(/[<>"'&]/g, '').trim();
}

function normalizeNodeModelPreferences(value: unknown): Record<string, string[]> {
    if (!value || typeof value !== 'object') return {};
    const normalized: Record<string, string[]> = {};
    for (const [rawRole, rawChain] of Object.entries(value as Record<string, unknown>)) {
        const role = String(rawRole || '').trim();
        if (!role) continue;
        const source = Array.isArray(rawChain)
            ? rawChain
            : typeof rawChain === 'string'
                ? rawChain.split(',')
                : [];
        const chain = source
            .map(item => String(item || '').trim())
            .filter(Boolean)
            .filter((item, index, arr) => arr.indexOf(item) === index)
            .slice(0, 6);
        if (chain.length > 0) normalized[role] = chain;
    }
    return normalized;
}

export default function SettingsModal({
    open, onClose, lang, onLangChange, theme, onThemeChange,
    connected, wsUrl, onWsUrlChange, wsRef,
}: SettingsModalProps) {
    const [tab, setTab] = useState<TabId>('conn');
    const [apiKeys, setApiKeys] = useState<ApiKeys>({ kimi: '', gemini: '', openai: '', claude: '', deepseek: '', qwen: '' });
    const [apiBases, setApiBases] = useState<Record<string, string>>({ openai: '', claude: '', gemini: '', kimi: '', deepseek: '', qwen: '' });
    const [autoL2, setAutoL2] = useState(true);
    const [l4Pass, setL4Pass] = useState('godmode');
    const [allowedDirs, setAllowedDirs] = useState('~/Desktop, ~/Documents');
    const [maxFileSize, setMaxFileSize] = useState('50MB');
    const [saving, setSaving] = useState(false);
    const [saveStatus, setSaveStatus] = useState<'idle' | 'success' | 'error'>('idle');
    // Quality settings
    const [smokeEnabled, setSmokeEnabled] = useState(true);
    const [browserHeadful, setBrowserHeadful] = useState(false);
    const [forceVisibleReview, setForceVisibleReview] = useState(false);
    const [maxRetries, setMaxRetries] = useState(3);
    const [browserResearch, setBrowserResearch] = useState(false);
    const [comfyUiUrl, setComfyUiUrl] = useState('');
    const [comfyWorkflowTemplate, setComfyWorkflowTemplate] = useState('');
    const [imageBackendAvailable, setImageBackendAvailable] = useState(false);
    const [nodeModelPreferences, setNodeModelPreferences] = useState<Record<string, string[]>>({});
    const [modelCatalog, setModelCatalog] = useState<ModelCatalogItem[]>([]);
    // Track which keys the user is actively editing (show real value)
    const [editingKey, setEditingKey] = useState<string | null>(null);
    // Track which keys have been loaded from backend (display as masked)
    const [loadedKeys, setLoadedKeys] = useState<Set<string>>(new Set());

    // Load settings from backend on open — only load has_keys flags, NOT the masked values
    useEffect(() => {
        if (!open) return;
        fetch(`${API_BASE}/api/settings`, { credentials: 'omit' })
            .then(r => r.json())
            .then(data => {
                // Backend returns has_keys: {openai_api_key: true, ...}
                // We use this to show which keys are already configured
                if (data.has_keys) {
                    const loaded = new Set<string>();
                    // Backend returns has_keys with short names: {openai: true, kimi: true, ...}
                    const keyMapping: Record<string, string> = {
                        openai_api_key: 'openai', openai: 'openai',
                        anthropic_api_key: 'claude', anthropic: 'claude',
                        gemini_api_key: 'gemini', gemini: 'gemini',
                        kimi_api_key: 'kimi', kimi: 'kimi',
                        deepseek_api_key: 'deepseek', deepseek: 'deepseek',
                        qwen_api_key: 'qwen', qwen: 'qwen',
                    };
                    for (const [backendKey, hasKey] of Object.entries(data.has_keys)) {
                        const frontendKey = keyMapping[backendKey];
                        if (frontendKey && hasKey) loaded.add(frontendKey);
                    }
                    setLoadedKeys(loaded);
                }
                // Load quality settings
                if (typeof data.builder_enable_browser === 'boolean') setBrowserResearch(data.builder_enable_browser);
                if (typeof data.tester_run_smoke === 'boolean') setSmokeEnabled(data.tester_run_smoke);
                if (typeof data.browser_headful === 'boolean') setBrowserHeadful(data.browser_headful);
                if (typeof data.reviewer_tester_force_headful === 'boolean') setForceVisibleReview(data.reviewer_tester_force_headful);
                if (typeof data.max_retries === 'number') setMaxRetries(Math.max(1, Math.min(8, data.max_retries)));
                if (data.image_generation && typeof data.image_generation === 'object') {
                    const imageCfg = data.image_generation as Record<string, unknown>;
                    if (typeof imageCfg.comfyui_url === 'string') setComfyUiUrl(imageCfg.comfyui_url);
                    if (typeof imageCfg.workflow_template === 'string') setComfyWorkflowTemplate(imageCfg.workflow_template);
                }
                if (typeof data.image_generation_available === 'boolean') setImageBackendAvailable(data.image_generation_available);
                if (data.node_model_preferences && typeof data.node_model_preferences === 'object') {
                    setNodeModelPreferences(normalizeNodeModelPreferences(data.node_model_preferences));
                }
                if (Array.isArray(data.model_catalog)) {
                    const catalog: ModelCatalogItem[] = data.model_catalog
                        .map((item: unknown) => ({
                            id: String((item as Record<string, unknown>).id || '').trim(),
                            provider: String((item as Record<string, unknown>).provider || '').trim(),
                        }))
                        .filter((item: ModelCatalogItem) => item.id.length > 0);
                    if (catalog.length > 0) setModelCatalog(catalog);
                }
                // Load relay base URLs
                if (data.api_bases && typeof data.api_bases === 'object') {
                    const bases: Record<string, string> = { openai: '', claude: '', gemini: '', kimi: '', deepseek: '', qwen: '' };
                    const baseMapping: Record<string, string> = {
                        openai: 'openai', anthropic: 'claude', gemini: 'gemini',
                        kimi: 'kimi', deepseek: 'deepseek', qwen: 'qwen',
                    };
                    for (const [bk, bv] of Object.entries(data.api_bases)) {
                        const fk = baseMapping[bk] || bk;
                        if (fk in bases && typeof bv === 'string') bases[fk] = bv;
                    }
                    setApiBases(bases);
                }
            }).catch(() => { /* offline */ });
    }, [open]);

    // Clear sensitive state on close
    const handleClose = useCallback(() => {
        setEditingKey(null);
        setSaveStatus('idle');
        onClose();
    }, [onClose]);
    // Available models after save
    const [availableModels, setAvailableModels] = useState<Record<string, string[]>>({});

    const saveToBackend = useCallback(async () => {
        setSaving(true);
        setSaveStatus('idle');

        // Build REST payload — settings.py expects api_keys.{provider}: "sk-..."
        const restKeys: Record<string, string> = {};
        // Build WS payload — server.py update_config expects {provider}_api_key: "sk-..."
        const wsKeys: Record<string, string> = {};
        const mapping: [keyof ApiKeys, string, string][] = [
            ['openai', 'openai', 'openai_api_key'],
            ['claude', 'anthropic', 'anthropic_api_key'],
            ['gemini', 'gemini', 'gemini_api_key'],
            ['kimi', 'kimi', 'kimi_api_key'],
            ['deepseek', 'deepseek', 'deepseek_api_key'],
            ['qwen', 'qwen', 'qwen_api_key'],
        ];
        for (const [uiKey, restKey, wsKey] of mapping) {
            if (apiKeys[uiKey]) {
                restKeys[restKey] = apiKeys[uiKey];
                wsKeys[wsKey] = apiKeys[uiKey];
            }
        }

        try {
            // 1) Save to disk via REST API (settings.py format)
            const resp = await fetch(`${API_BASE}/api/settings/save`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'omit',
                body: JSON.stringify({
                    api_keys: restKeys,
                    api_bases: {
                        openai: apiBases.openai || '',
                        anthropic: apiBases.claude || '',
                        gemini: apiBases.gemini || '',
                        kimi: apiBases.kimi || '',
                        deepseek: apiBases.deepseek || '',
                        qwen: apiBases.qwen || '',
                    },
                    builder_enable_browser: browserResearch,
                    tester_run_smoke: smokeEnabled,
                    browser_headful: browserHeadful,
                    reviewer_tester_force_headful: forceVisibleReview,
                    max_retries: maxRetries,
                    image_generation: {
                        comfyui_url: comfyUiUrl,
                        workflow_template: comfyWorkflowTemplate,
                    },
                    node_model_preferences: nodeModelPreferences,
                }),
            });

            if (resp.ok) {
                const result = await resp.json();

                // 2) Also update the live WebSocket session so keys take effect immediately
                if (wsRef?.current && wsRef.current.readyState === WebSocket.OPEN) {
                    wsRef.current.send(JSON.stringify({
                        type: 'update_config',
                        config: {
                            ...wsKeys,
                            builder_enable_browser: browserResearch,
                            tester_run_smoke: smokeEnabled,
                            browser_headful: browserHeadful,
                            reviewer_tester_force_headful: forceVisibleReview,
                            max_retries: maxRetries,
                            image_generation: {
                                comfyui_url: comfyUiUrl,
                                workflow_template: comfyWorkflowTemplate,
                            },
                            node_model_preferences: nodeModelPreferences,
                        },
                    }));
                }

                // 3) Show available models
                if (result.available_models) {
                    setAvailableModels(result.available_models);
                }
                if (typeof result.image_generation_available === 'boolean') {
                    setImageBackendAvailable(result.image_generation_available);
                }
                if (result.node_model_preferences && typeof result.node_model_preferences === 'object') {
                    setNodeModelPreferences(normalizeNodeModelPreferences(result.node_model_preferences));
                }

                setSaveStatus('success');
                const loaded = new Set<string>();
                for (const [k, v] of Object.entries(apiKeys)) {
                    if (v) loaded.add(k);
                }
                setLoadedKeys(loaded);
                setEditingKey(null);
            } else {
                setSaveStatus('error');
            }
        } catch {
            setSaveStatus('error');
        }
        setSaving(false);
        setTimeout(() => setSaveStatus('idle'), 5000);
    }, [apiKeys, apiBases, wsRef, browserResearch, smokeEnabled, browserHeadful, forceVisibleReview, maxRetries, comfyUiUrl, comfyWorkflowTemplate, nodeModelPreferences]);

    // Handle key input change with sanitization
    const handleKeyChange = useCallback((key: keyof ApiKeys, value: string) => {
        const sanitized = sanitizeInput(value);
        setApiKeys(prev => ({ ...prev, [key]: sanitized }));
    }, []);

    // Prevent key display via browser DevTools / autocomplete
    const keyInputProps = {
        autoComplete: 'off' as const,
        'data-lpignore': 'true', // LastPass ignore
        'data-1p-ignore': 'true', // 1Password ignore
        spellCheck: false as const,
    };

    const modelOptions = (modelCatalog.length > 0 ? modelCatalog : FALLBACK_MODEL_CATALOG)
        .slice()
        .sort((a, b) => {
            const providerCompare = a.provider.localeCompare(b.provider);
            return providerCompare !== 0 ? providerCompare : a.id.localeCompare(b.id);
        });

    const updateNodeModelPreference = useCallback((role: string, slotIndex: number, nextModel: string) => {
        setNodeModelPreferences((prev) => {
            const next = { ...prev };
            const current = [...(next[role] || [])];
            current[slotIndex] = nextModel;
            const normalized = current
                .map((item) => String(item || '').trim())
                .filter(Boolean)
                .filter((item, index, arr) => arr.indexOf(item) === index)
                .slice(0, 6);
            if (normalized.length > 0) {
                next[role] = normalized;
            } else {
                delete next[role];
            }
            return next;
        });
    }, []);

    if (!open) return null;

    const t = (en: string, zh: string) => lang === 'zh' ? zh : en;

    return (
        <div className="modal-overlay" onClick={handleClose}>
            <div className="modal-container" onClick={e => e.stopPropagation()}>
                {/* Header */}
                <div className="modal-header">
                    <h3>{t('Settings', '设置')}</h3>
                    <button className="modal-close" onClick={handleClose}>✕</button>
                </div>

                {/* Tabs */}
                <div className="settings-tabs">
                    {TABS.map(tb => (
                        <button
                            key={tb.id}
                            className={`settings-tab ${tab === tb.id ? 'active' : ''}`}
                            onClick={() => setTab(tb.id)}
                        >
                            {lang === 'zh' ? tb.label_zh : tb.label_en}
                        </button>
                    ))}
                </div>

                {/* Body */}
                <div className="modal-body">
                    {tab === 'conn' && (
                        <>
                            {/* Connection Status */}
                            <div className="s-section">
                                <div className="s-section-title">{t('Connection Status', '连接状态')}</div>
                                <div className="s-row">
                                    <span className={`s-dot ${connected ? 'on' : 'off'}`} />
                                    <span className={`s-badge ${connected ? 'green' : 'red'}`}>
                                        {connected ? t('Connected', '已连接') : t('Disconnected', '未连接')}
                                    </span>
                                </div>
                                <div className="s-row">
                                    <label>WebSocket URL</label>
                                    <input
                                        className="s-input"
                                        value={wsUrl}
                                        onChange={e => onWsUrlChange(e.target.value)}
                                    />
                                </div>
                            </div>

                            {/* API Keys */}
                            <div className="s-section">
                                <div className="s-section-title">{t('API Keys', 'API 密钥')}</div>
                                <div className="s-hint" style={{ marginBottom: 8 }}>
                                    {t(
                                        'Keys are sent directly to your local backend. Never shared with third parties. For relay/proxy APIs, fill in the Base URL below each key.',
                                        '密钥仅发送到本地后端，绝不会发送给第三方。如使用中转 API，请在每个密钥下方填写中转地址（Base URL）。'
                                    )}
                                </div>
                                {(['openai', 'claude', 'gemini', 'kimi', 'deepseek', 'qwen'] as const).map(k => {
                                    const isEditing = editingKey === k;
                                    const hasValue = !!apiKeys[k];
                                    const isConfigured = loadedKeys.has(k);
                                    const displayValue = isEditing ? apiKeys[k] : (hasValue ? maskKey(apiKeys[k]) : '');
                                    return (
                                        <div key={k} style={{ marginBottom: 6 }}>
                                            <div className="s-row">
                                                <label>{k.charAt(0).toUpperCase() + k.slice(1)}</label>
                                                <input
                                                    className="s-input"
                                                    type={isEditing ? 'text' : 'password'}
                                                    value={displayValue}
                                                    onChange={e => handleKeyChange(k, e.target.value)}
                                                    onFocus={() => setEditingKey(k)}
                                                    onBlur={() => setEditingKey(null)}
                                                    placeholder={isConfigured && !hasValue ? '••• (已配置)' : 'sk-...'}
                                                    {...keyInputProps}
                                                />
                                                {(hasValue || isConfigured) && (
                                                    <span style={{ fontSize: 8, color: 'var(--green)', fontWeight: 600 }}>
                                                        {hasValue ? '✓ 新' : '✓'}
                                                    </span>
                                                )}
                                            </div>
                                            <div className="s-row" style={{ paddingLeft: 16, opacity: 0.85, marginTop: 2 }}>
                                                <label style={{ fontSize: 9, color: 'var(--text3)' }}>
                                                    {t('Base URL', '中转地址')}
                                                </label>
                                                <input
                                                    className="s-input"
                                                    value={apiBases[k] || ''}
                                                    onChange={e => setApiBases(prev => ({ ...prev, [k]: sanitizeInput(e.target.value) }))}
                                                    placeholder={t('Leave empty for official endpoint', '留空使用官方地址')}
                                                    style={{ fontSize: 10 }}
                                                />
                                            </div>
                                        </div>
                                    );
                                })}
                                <div className="flex items-center gap-2 mt-2">
                                    <button className="btn btn-primary text-[10px]" onClick={saveToBackend} disabled={saving}>
                                        {saving ? t('Saving...', '保存中...') : t('Save to Backend', '保存到后端')}
                                    </button>
                                    {saveStatus === 'success' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                    {saveStatus === 'error' && <span className="text-[9px]" style={{ color: 'var(--red)' }}>✗ {t('Failed', '保存失败')}</span>}
                                </div>

                                {/* Available models after save */}
                                {Object.keys(availableModels).length > 0 && (
                                    <div className="s-section" style={{ marginTop: 12, padding: '8px 10px', background: 'rgba(79,143,255,0.05)', borderRadius: 8, border: '1px solid rgba(79,143,255,0.15)' }}>
                                        <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--blue)', marginBottom: 6 }}>
                                            {t('Available Models', '可用模型')}
                                        </div>
                                        {Object.entries(availableModels).map(([provider, models]) => {
                                            const providerNames: Record<string, string> = {
                                                openai: 'OpenAI', anthropic: 'Claude', google: 'Gemini',
                                                deepseek: 'DeepSeek', kimi: 'Kimi', qwen: '通义千问', ollama: 'Ollama',
                                            };
                                            return (
                                                <div key={provider} style={{ marginBottom: 4 }}>
                                                    <span style={{ fontSize: 9, color: 'var(--green)', fontWeight: 600 }}>
                                                        {providerNames[provider] || provider}:
                                                    </span>
                                                    <span style={{ fontSize: 9, color: 'var(--text2)', marginLeft: 4 }}>
                                                        {(models as string[]).join(', ')}
                                                    </span>
                                                </div>
                                            );
                                        })}
                                        <div style={{ fontSize: 8, color: 'var(--text3)', marginTop: 4 }}>
                                            {t('You can select these models on each node', '你可以在每个节点上选择这些模型')}
                                        </div>
                                    </div>
                                )}
                            </div>
                        </>
                    )}

                    {tab === 'perm' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">{t('Security Config', '安全配置')}</div>
                                <div className="s-row">
                                    <label>L4 {t('Password', '密码')}</label>
                                    <input className="s-input" type="password" value={l4Pass} onChange={e => setL4Pass(e.target.value)} autoComplete="off" />
                                </div>
                                <div className="s-toggle">
                                    <label>{t('Auto-approve L2 ops', '自动批准L2操作')}</label>
                                    <input type="checkbox" checked={autoL2} onChange={e => setAutoL2(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    L1: {t('Read-only', '只读')} · L2: {t('File/Network', '文件/网络')} · L3: {t('Confirm required', '需确认')} · L4: {t('Password required', '需密码')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Execution Whitelist', '执行白名单')}</div>
                                <div className="s-row">
                                    <label>{t('Allowed Dirs', '允许目录')}</label>
                                    <input className="s-input" value={allowedDirs} onChange={e => setAllowedDirs(e.target.value)} />
                                </div>
                                <div className="s-row">
                                    <label>{t('Max File Size', '最大文件')}</label>
                                    <input className="s-input" value={maxFileSize} onChange={e => setMaxFileSize(e.target.value)} style={{ width: 80, flex: 'none' }} />
                                </div>
                            </div>
                        </>
                    )}

                    {tab === 'quality' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">{t('Visual Verification', '视觉验收')}</div>
                                <div className="s-toggle">
                                    <label>{t('Playwright Smoke Test', 'Playwright 烟雾测试')}</label>
                                    <input type="checkbox" checked={smokeEnabled} onChange={e => setSmokeEnabled(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {t('Run headless browser test to validate rendered page', '运行无头浏览器测试验证页面渲染效果')}
                                </div>
                                <div className="s-toggle" style={{ marginTop: 8 }}>
                                    <label>{t('Visible Browser Window (Headful)', '可见浏览器窗口 (Headful)')}</label>
                                    <input type="checkbox" checked={browserHeadful} onChange={e => setBrowserHeadful(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {browserHeadful
                                        ? t('Enabled: reviewer/tester browser actions open a visible Chromium window (slower but observable).', '已开启：审查员/测试员浏览器动作会打开可见 Chromium 窗口（更慢但可观察）。')
                                        : t('Disabled: browser actions run in headless mode (faster, silent).', '已关闭：浏览器动作使用无头模式（更快、静默）。')}
                                </div>
                                <div className="s-toggle" style={{ marginTop: 8 }}>
                                    <label>{t('Force Visible Browser for Reviewer/Tester', '审查员/测试员强制可见浏览器')}</label>
                                    <input type="checkbox" checked={forceVisibleReview} onChange={e => setForceVisibleReview(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {forceVisibleReview
                                        ? t('Enabled: reviewer/tester stages force browser headful mode even if global mode is headless.', '已开启：即使全局是无头模式，审查员/测试员阶段也会强制可见窗口。')
                                        : t('Disabled: reviewer/tester follow global headful/headless mode.', '已关闭：审查员/测试员跟随全局浏览器模式。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Retry Strategy', '重试策略')}</div>
                                <div className="s-row">
                                    <label>{t('Max Retries', '最大重试')}</label>
                                    <select className="s-input" value={maxRetries} onChange={e => setMaxRetries(Number(e.target.value))} style={{ width: 'auto' }}>
                                        <option value={1}>1</option>
                                        <option value={2}>2</option>
                                        <option value={3}>3 ({t('default', '默认')})</option>
                                        <option value={5}>5</option>
                                    </select>
                                </div>
                                <div className="s-hint">
                                    {t('Failures auto-downgrade model. If a node has its own chain in "Node Models", that chain takes priority.',
                                       '失败时会自动降级模型；如果你在“节点模型”里给某个节点单独配置了链路，则优先按那个链路执行。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Builder Enhancement', 'Builder 增强')}</div>
                                <div className="s-toggle">
                                    <label>{t('Web Research Mode', '联网参考模式')}</label>
                                    <input type="checkbox" checked={browserResearch} onChange={e => setBrowserResearch(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {t('Allow builder to browse 1-2 reference pages for design inspiration',
                                       '允许 builder 浏览 1-2 个参考页面获取设计灵感')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Image Generation Backend', '图像生成后端')}</div>
                                <div className="s-row">
                                    <label>ComfyUI URL</label>
                                    <input
                                        className="s-input"
                                        value={comfyUiUrl}
                                        onChange={e => setComfyUiUrl(sanitizeInput(e.target.value))}
                                        placeholder="http://127.0.0.1:8188"
                                    />
                                </div>
                                <div className="s-row">
                                    <label>{t('Workflow Template', '工作流模板')}</label>
                                    <input
                                        className="s-input"
                                        value={comfyWorkflowTemplate}
                                        onChange={e => setComfyWorkflowTemplate(sanitizeInput(e.target.value))}
                                        placeholder="/path/to/workflow.json"
                                    />
                                </div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {imageBackendAvailable
                                        ? t('Configured: planner may auto-insert imagegen / spritesheet / assetimport for asset-heavy goals.',
                                            '已配置：规划器在素材密集型任务里会自动插入 imagegen / spritesheet / assetimport。')
                                        : t('Not configured: image nodes will not be auto-inserted. The system will fall back to prompt packs and placeholder assets.',
                                            '未配置：系统不会自动插入图像节点，会退回到提示词包和占位素材方案。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Model Strategy', '模型策略')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.6 }}>
                                    Simple → {t('fastest model (kimi/deepseek)', '最快模型 (kimi/deepseek)')}<br/>
                                    Standard → {t('default model', '默认模型')}<br/>
                                    Pro → {t('strongest model (gpt-5.4/claude-4)', '最强模型 (gpt-5.4/claude-4)')}<br/>
                                    <span style={{ fontSize: 8, color: 'var(--text3)' }}>
                                        {t('Auto-selects based on configured API keys', '根据已配置的 API 密钥自动选择')}
                                    </span>
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('What these settings mean', '这些设置具体是什么意思')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    • {t('Smoke Test: visual browser check in tester stage. Off = faster, On = stricter page validation.',
                                           'Smoke Test：Tester 阶段做真实浏览器可视化检查。关闭更快，开启更严格。')}<br/>
                                    • {t('Max Retries: max retry count after a subtask fails. Higher = higher success chance, but slower.',
                                           'Max Retries：子任务失败后的最大重试次数。值越大成功率通常更高，但更慢。')}<br/>
                                    • {t('Web Research Mode: allows Builder to browse references for better UI inspiration.',
                                           '联网参考模式：允许 Builder 浏览参考页面，提升 UI 设计灵感。')}<br/>
                                    • {t('Model Strategy: Simple for speed, Standard for balance, Pro for best quality.',
                                           '模型策略：Simple 主打速度，Standard 平衡，Pro 追求最高质量。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Browser Engine Permissions', '浏览器引擎权限')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    <b>{t('Headless mode (default)', 'Headless 模式（默认）')}</b><br/>
                                    • {t('No macOS permissions required', '不需要任何 macOS 系统权限')}<br/>
                                    • {t('Runs silently in background, takes screenshots automatically', '后台静默运行，自动截图分析')}<br/>
                                    • {t('Requires:', '需要安装：')} <code>python3 -m playwright install chromium</code><br/><br/>
                                    <b>{t('Headful mode (visible browser)', 'Headful 模式（可见浏览器）')}</b><br/>
                                    • {t('For Playwright page automation, macOS Accessibility/Screen Recording is usually NOT required', '仅 Playwright 页面自动化时，通常不需要 macOS 辅助功能/屏幕录制权限')}<br/>
                                    • {t('Desktop-control of other apps (not this browser test) may require those permissions', '如果是控制其他桌面应用（非本浏览器测试），才可能需要这些权限')}
                                </div>
                            </div>
                            <div className="flex items-center gap-2 mt-2">
                                <button className="btn btn-primary text-[10px]" onClick={saveToBackend} disabled={saving}>
                                    {saving ? t('Saving...', '保存中...') : t('Save Quality Settings', '保存质量设置')}
                                </button>
                                {saveStatus === 'success' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                {saveStatus === 'error' && <span className="text-[9px]" style={{ color: 'var(--red)' }}>✗ {t('Failed', '保存失败')}</span>}
                            </div>
                        </>
                    )}

                    {tab === 'nodes' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">{t('Per-Node Model Fallback', '节点专属模型回退链')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {t(
                                        'Each node can define its own model priority chain. The runtime will try Priority 1 first, then automatically fall back to the next model on missing key, auth/provider failure, timeout, or network error.',
                                        '每个节点都可以单独定义模型优先级链。运行时会先尝试优先级 1，如果缺少 Key、鉴权失败、提供商异常、超时或网络错误，会自动回退到下一个模型。'
                                    )}
                                </div>
                                <div className="s-hint" style={{ lineHeight: 1.7, marginTop: 6 }}>
                                    {t(
                                        'If Priority 1 is empty, that node keeps using the current global/default model strategy.',
                                        '如果优先级 1 留空，该节点会继续使用当前全局/默认模型策略。'
                                    )}
                                </div>
                            </div>
                            {NODE_MODEL_CONFIG_ROLES.map((role) => {
                                const info = NODE_TYPES[role];
                                const chain = nodeModelPreferences[role] || [];
                                const nodeLabel = lang === 'zh'
                                    ? info?.label_zh || role
                                    : info?.label_en || role;
                                return (
                                    <div key={role} className="s-section">
                                        <div className="s-section-title">{nodeLabel}</div>
                                        {NODE_MODEL_PRIORITY_SLOTS.map((slotIndex) => (
                                            <div className="s-row" key={`${role}-${slotIndex}`}>
                                                <label>{t(`Priority ${slotIndex + 1}`, `优先级 ${slotIndex + 1}`)}</label>
                                                <select
                                                    className="s-input"
                                                    value={chain[slotIndex] || ''}
                                                    onChange={(e) => updateNodeModelPreference(role, slotIndex, e.target.value)}
                                                >
                                                    <option value="">
                                                        {slotIndex === 0
                                                            ? t('Use global/default', '使用全局/默认')
                                                            : t('No fallback', '不设置回退')}
                                                    </option>
                                                    {modelOptions.map((item) => (
                                                        <option key={`${role}-${item.id}`} value={item.id}>
                                                            {item.id} ({PROVIDER_LABELS[item.provider] || item.provider || 'Model'})
                                                        </option>
                                                    ))}
                                                </select>
                                            </div>
                                        ))}
                                        <div className="s-hint" style={{ lineHeight: 1.6 }}>
                                            {chain.length > 0
                                                ? (
                                                    lang === 'zh'
                                                        ? `执行顺序：${chain.join(' → ')}`
                                                        : `Execution order: ${chain.join(' → ')}`
                                                )
                                                : t(
                                                    'No dedicated chain configured for this node.',
                                                    '这个节点当前还没有单独配置模型链。'
                                                )}
                                        </div>
                                    </div>
                                );
                            })}
                            <div className="flex items-center gap-2 mt-2">
                                <button className="btn btn-primary text-[10px]" onClick={saveToBackend} disabled={saving}>
                                    {saving ? t('Saving...', '保存中...') : t('Save Node Model Rules', '保存节点模型规则')}
                                </button>
                                {saveStatus === 'success' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                {saveStatus === 'error' && <span className="text-[9px]" style={{ color: 'var(--red)' }}>✗ {t('Failed', '保存失败')}</span>}
                            </div>
                        </>
                    )}

                    {tab === 'ui' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">{t('Language', '语言')}</div>
                                <div className="s-row">
                                    <label>{t('Language', '语言')}</label>
                                    <select className="s-input" value={lang} onChange={e => onLangChange(e.target.value as 'en' | 'zh')} style={{ width: 'auto' }}>
                                        <option value="zh">中文</option>
                                        <option value="en">English</option>
                                    </select>
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Theme', '主题')}</div>
                                <div className="s-row">
                                    <label>{t('Theme', '主题')}</label>
                                    <select className="s-input" value={theme} onChange={e => onThemeChange(e.target.value as 'dark' | 'light')} style={{ width: 'auto' }}>
                                        <option value="dark">{t('Dark', '深色')}</option>
                                        <option value="light">{t('Light', '浅色')}</option>
                                    </select>
                                </div>
                            </div>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}
