'use client';

import { useState, useEffect, useCallback } from 'react';

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

type TabId = 'conn' | 'perm' | 'ui' | 'quality';

const TABS: { id: TabId; icon: string; label_en: string; label_zh: string }[] = [
    { id: 'conn', icon: '🔌', label_en: 'Connection', label_zh: '连接' },
    { id: 'perm', icon: '🔐', label_en: 'Permissions', label_zh: '权限' },
    { id: 'quality', icon: '🔬', label_en: 'Quality', label_zh: '验收策略' },
    { id: 'ui', icon: '🎨', label_en: 'Interface', label_zh: '界面' },
];

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8765';

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

export default function SettingsModal({
    open, onClose, lang, onLangChange, theme, onThemeChange,
    connected, wsUrl, onWsUrlChange, wsRef,
}: SettingsModalProps) {
    const [tab, setTab] = useState<TabId>('conn');
    const [apiKeys, setApiKeys] = useState<ApiKeys>({ kimi: '', gemini: '', openai: '', claude: '', deepseek: '', qwen: '' });
    const [autoL2, setAutoL2] = useState(true);
    const [l4Pass, setL4Pass] = useState('godmode');
    const [allowedDirs, setAllowedDirs] = useState('~/Desktop, ~/Documents');
    const [maxFileSize, setMaxFileSize] = useState('50MB');
    const [saving, setSaving] = useState(false);
    const [saveStatus, setSaveStatus] = useState<'idle' | 'success' | 'error'>('idle');
    // Quality settings
    const [smokeEnabled, setSmokeEnabled] = useState(true);
    const [browserHeadful, setBrowserHeadful] = useState(false);
    const [forceVisibleReview, setForceVisibleReview] = useState(true);
    const [maxRetries, setMaxRetries] = useState(3);
    const [browserResearch, setBrowserResearch] = useState(false);
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
                    builder_enable_browser: browserResearch,
                    tester_run_smoke: smokeEnabled,
                    browser_headful: browserHeadful,
                    reviewer_tester_force_headful: forceVisibleReview,
                    max_retries: maxRetries,
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
                        },
                    }));
                }

                // 3) Show available models
                if (result.available_models) {
                    setAvailableModels(result.available_models);
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
    }, [apiKeys, wsRef, browserResearch, smokeEnabled, browserHeadful, forceVisibleReview, maxRetries]);

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

    if (!open) return null;

    const t = (en: string, zh: string) => lang === 'zh' ? zh : en;

    return (
        <div className="modal-overlay" onClick={handleClose}>
            <div className="modal-container" onClick={e => e.stopPropagation()}>
                {/* Header */}
                <div className="modal-header">
                    <h3>⚙️ {t('Settings', '设置')}</h3>
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
                            {tb.icon} {lang === 'zh' ? tb.label_zh : tb.label_en}
                        </button>
                    ))}
                </div>

                {/* Body */}
                <div className="modal-body">
                    {tab === 'conn' && (
                        <>
                            {/* Connection Status */}
                            <div className="s-section">
                                <div className="s-section-title">📡 {t('Connection Status', '连接状态')}</div>
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
                                <div className="s-section-title">🔑 {t('API Keys', 'API 密钥')}</div>
                                <div className="s-hint" style={{ marginBottom: 8 }}>
                                    🔒 {t('Keys are sent directly to your local backend. Never shared with third parties.', '密钥仅发送到本地后端，绝不会发送给第三方。')}
                                </div>
                                {(['openai', 'claude', 'gemini', 'kimi', 'deepseek', 'qwen'] as const).map(k => {
                                    const isEditing = editingKey === k;
                                    const hasValue = !!apiKeys[k];
                                    const isConfigured = loadedKeys.has(k); // key exists on backend
                                    const displayValue = isEditing ? apiKeys[k] : (hasValue ? maskKey(apiKeys[k]) : '');
                                    return (
                                        <div className="s-row" key={k}>
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
                                                <span style={{
                                                    fontSize: 8, color: 'var(--green)', fontWeight: 600,
                                                }}>{hasValue ? '✓ 新' : '✓'}</span>
                                            )}
                                        </div>
                                    );
                                })}
                                <div className="flex items-center gap-2 mt-2">
                                    <button className="btn btn-primary text-[10px]" onClick={saveToBackend} disabled={saving}>
                                        {saving ? '⏳' : '☁️'} {t('Save to Backend', '保存到后端')}
                                    </button>
                                    {saveStatus === 'success' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                    {saveStatus === 'error' && <span className="text-[9px]" style={{ color: 'var(--red)' }}>✗ {t('Failed', '保存失败')}</span>}
                                </div>

                                {/* Available models after save */}
                                {Object.keys(availableModels).length > 0 && (
                                    <div className="s-section" style={{ marginTop: 12, padding: '8px 10px', background: 'rgba(79,143,255,0.05)', borderRadius: 8, border: '1px solid rgba(79,143,255,0.15)' }}>
                                        <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--blue)', marginBottom: 6 }}>
                                            🤖 {t('Available Models', '可用模型')}
                                        </div>
                                        {Object.entries(availableModels).map(([provider, models]) => {
                                            const providerNames: Record<string, string> = {
                                                openai: 'OpenAI', anthropic: 'Claude', google: 'Gemini',
                                                deepseek: 'DeepSeek', kimi: 'Kimi', qwen: '通义千问', ollama: 'Ollama',
                                            };
                                            return (
                                                <div key={provider} style={{ marginBottom: 4 }}>
                                                    <span style={{ fontSize: 9, color: 'var(--green)', fontWeight: 600 }}>
                                                        ✅ {providerNames[provider] || provider}:
                                                    </span>
                                                    <span style={{ fontSize: 9, color: 'var(--text2)', marginLeft: 4 }}>
                                                        {(models as string[]).join(', ')}
                                                    </span>
                                                </div>
                                            );
                                        })}
                                        <div style={{ fontSize: 8, color: 'var(--text3)', marginTop: 4 }}>
                                            💡 {t('You can select these models on each node', '你可以在每个节点上选择这些模型')}
                                        </div>
                                    </div>
                                )}
                            </div>
                        </>
                    )}

                    {tab === 'perm' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">🔐 {t('Security Config', '安全配置')}</div>
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
                                <div className="s-section-title">📋 {t('Execution Whitelist', '执行白名单')}</div>
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
                                <div className="s-section-title">🔬 {t('Visual Verification', '视觉验收')}</div>
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
                                <div className="s-section-title">🔄 {t('Retry Strategy', '重试策略')}</div>
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
                                    {t('Failures auto-downgrade model: gpt-5.4 → claude-4 → kimi → deepseek → gemini-flash → qwen',
                                       '失败自动降级模型：gpt-5.4 → claude-4 → kimi → deepseek → gemini-flash → qwen')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">🌐 {t('Builder Enhancement', 'Builder 增强')}</div>
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
                                <div className="s-section-title">⚡ {t('Model Strategy', '模型策略')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.6 }}>
                                    ⚡ Simple → {t('fastest model (kimi/deepseek)', '最快模型 (kimi/deepseek)')}<br/>
                                    🔥 Standard → {t('default model', '默认模型')}<br/>
                                    💎 Pro → {t('strongest model (gpt-5.4/claude-4)', '最强模型 (gpt-5.4/claude-4)')}<br/>
                                    <span style={{ fontSize: 8, color: 'var(--text3)' }}>
                                        {t('Auto-selects based on configured API keys', '根据已配置的 API 密钥自动选择')}
                                    </span>
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">🧭 {t('What these settings mean', '这些设置具体是什么意思')}</div>
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
                                <div className="s-section-title">🖥️ {t('Browser Engine Permissions', '浏览器引擎权限')}</div>
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
                        </>
                    )}

                    {tab === 'ui' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">🌐 {t('Language', '语言')}</div>
                                <div className="s-row">
                                    <label>{t('Language', '语言')}</label>
                                    <select className="s-input" value={lang} onChange={e => onLangChange(e.target.value as 'en' | 'zh')} style={{ width: 'auto' }}>
                                        <option value="zh">中文</option>
                                        <option value="en">English</option>
                                    </select>
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">🎨 {t('Theme', '主题')}</div>
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
