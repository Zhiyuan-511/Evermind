'use client';

interface ToolbarProps {
    workflowName: string;
    onNameChange: (name: string) => void;
    onRun: () => void;
    onStop: () => void;
    onExport: () => void;
    onClear: () => void;
    running: boolean;
    connected: boolean;
    lang: 'en' | 'zh';
    onLangToggle: () => void;
    theme: 'dark' | 'light';
    onThemeToggle: () => void;
    onOpenSettings: () => void;
    onOpenTemplates: () => void;
    onOpenSkillsLibrary: () => void;
    onOpenGuide: () => void;
    onOpenHistory: () => void;
    onOpenDiagnostics: () => void;
    /* Canvas view toggle */
    canvasView: 'editor' | 'preview';
    onToggleCanvasView: () => void;
    hasPreview: boolean;
    /* P0-2: OpenClaw status bar */
    activeRunStatus?: string;
    runtimeModeLabel?: string;
    activeTaskLabel?: string;
    activeRunId?: string;
    lastEventAt?: number | null;
    /* §2.1: Instance badge */
    wsUrl?: string;
    envTag?: string;
    onOpenConnectorPanel?: () => void;
}

export default function Toolbar({
    workflowName, onNameChange, onRun, onStop, onExport, onClear,
    running, connected, lang, onLangToggle, theme, onThemeToggle,
    onOpenSettings, onOpenTemplates, onOpenGuide,
    onOpenSkillsLibrary,
    onOpenHistory, onOpenDiagnostics,
    canvasView, onToggleCanvasView, hasPreview,
    activeRunStatus, runtimeModeLabel, activeTaskLabel, activeRunId, lastEventAt, wsUrl, envTag, onOpenConnectorPanel,
}: ToolbarProps) {
    const tr = (zh: string, en: string) => lang === 'zh' ? zh : en;
    const humanizeRunStatus = (status?: string) => {
        const normalized = String(status || '').trim().toLowerCase();
        if (!normalized || normalized === 'idle') return tr('空闲', 'Idle');
        const zhMap: Record<string, string> = {
            queued: '排队中',
            running: '执行中',
            executing: '执行中',
            waiting_review: '等待审核',
            waiting_selfcheck: '等待自检',
            failed: '失败',
            done: '完成',
            cancelled: '已取消',
        };
        const enMap: Record<string, string> = {
            queued: 'Queued',
            running: 'Running',
            executing: 'Executing',
            waiting_review: 'Awaiting review',
            waiting_selfcheck: 'Awaiting self-check',
            failed: 'Failed',
            done: 'Done',
            cancelled: 'Cancelled',
        };
        return (lang === 'zh' ? zhMap : enMap)[normalized] || status || tr('空闲', 'Idle');
    };

    /* ── P0-2: OpenClaw Status Bar Logic ── */
    const statusDotColor = connected ? '#22c55e' : '#ef4444';
    const statusLabel = connected
        ? tr('已连接', 'Connected')
        : tr('离线', 'Offline');
    const runtimeLabel = String(runtimeModeLabel || 'local').trim() || 'local';
    const runLabel = humanizeRunStatus(activeRunStatus || (running ? 'running' : 'idle'));
    const taskLabel = String(activeTaskLabel || '').trim();
    const runIdLabel = String(activeRunId || '').trim();
    const eventLabel = lastEventAt
        ? new Date(lastEventAt).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-US', {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        })
        : '—';
    // §2.1: DEV/PACKAGED badge
    const instanceBadge = envTag === 'dev' ? 'DEV' : envTag === 'packaged' ? '' : '';
    // §2.2: Backend URL for diagnostics
    const wsUrlLabel = String(wsUrl || '').replace(/^ws:\/\//, '').replace(/\/ws$/, '') || '127.0.0.1:8765';

    return (
        <div className="glass-strong flex items-center gap-2 px-3 border-b border-white/5" style={{ height: 'var(--header-h)', overflowX: 'auto', overflowY: 'hidden' }}>
            {/* Workflow name / Active Task Title */}
            <input
                value={taskLabel || workflowName}
                onChange={e => !taskLabel && onNameChange(e.target.value)}
                readOnly={!!taskLabel}
                className="bg-transparent text-sm font-semibold text-[var(--text1)] border-none outline-none"
                placeholder={tr('工作流名称', 'Workflow name')}
                style={{ width: 120, minWidth: 50, flexShrink: 1, ...(taskLabel ? { color: 'var(--blue)', cursor: 'default' } : {}) }}
                title={taskLabel || workflowName}
            />

            <div className="h-4 w-px bg-white/10" style={{ flexShrink: 0 }} />

            {/* Run / Stop */}
            {running ? (
                <button onClick={onStop} className="btn btn-danger text-[11px]" style={{ flexShrink: 0 }}>
                    {tr('停止', 'Stop')}
                </button>
            ) : (
                <button onClick={onRun} className="btn btn-primary text-[11px]" style={{ flexShrink: 0 }}>
                    ▶ {tr('运行', 'Run')}
                </button>
            )}

            {/* ═══ Canvas View Toggle ═══ */}
            <div style={{
                display: 'flex', borderRadius: 8,
                border: '1px solid var(--glass-border)',
                overflow: 'hidden', flexShrink: 0,
            }}>
                <button
                    onClick={canvasView === 'editor' ? undefined : onToggleCanvasView}
                    style={{
                        padding: '4px 10px', fontSize: 10, fontWeight: 600,
                        border: 'none', cursor: 'pointer',
                        background: canvasView === 'editor' ? 'rgba(79,143,255,0.15)' : 'transparent',
                        color: canvasView === 'editor' ? 'var(--blue)' : 'var(--text3)',
                        transition: 'all 0.15s',
                    }}
                >
                    {tr('节点', 'Nodes')}
                </button>
                <button
                    onClick={canvasView === 'preview' ? undefined : onToggleCanvasView}
                    style={{
                        padding: '4px 10px', fontSize: 10, fontWeight: 600,
                        border: 'none', cursor: 'pointer',
                        background: canvasView === 'preview' ? 'rgba(64,214,124,0.15)' : 'transparent',
                        color: canvasView === 'preview' ? 'var(--green)' : 'var(--text3)',
                        transition: 'all 0.15s',
                        display: 'flex', alignItems: 'center', gap: 5,
                    }}
                >
                    {tr('预览', 'Preview')}
                    {hasPreview && (
                        <span style={{
                            width: 6, height: 6, borderRadius: '50%',
                            background: 'var(--green)',
                            boxShadow: '0 0 6px var(--green)',
                            flexShrink: 0,
                        }} />
                    )}
                </button>
            </div>

            <div className="h-4 w-px bg-white/10" style={{ flexShrink: 0 }} />

            {/* Actions — compacted */}
            <button onClick={onExport} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }}>
                {tr('导出', 'Export')}
            </button>
            <button onClick={onClear} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }}>
                {tr('清空', 'Clear')}
            </button>
            <button onClick={onOpenTemplates} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }}>
                {tr('模板', 'Tpl')}
            </button>
            <button onClick={onOpenSkillsLibrary} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }}>
                {tr('技能库', 'Skills')}
            </button>
            <button onClick={onOpenHistory} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }}>
                {tr('历史', 'Hist')}
            </button>
            <button onClick={onOpenDiagnostics} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }}>
                {tr('诊断', 'Diag')}
            </button>

            {/* Spacer */}
            <div style={{ flex: 1, minWidth: 4 }} />

            {/* Theme toggle */}
            <button onClick={onThemeToggle} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }}>
                {theme === 'dark' ? tr('白天', '☀') : tr('深夜', '🌙')}
            </button>

            {/* Lang toggle */}
            <button onClick={onLangToggle} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }}>
                {lang === 'zh' ? 'EN' : '中'}
            </button>

            {/* Settings */}
            <button onClick={onOpenSettings} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }} title={tr('设置', 'Settings')}>
                {tr('设置', '⚙')}
            </button>

            {/* Help */}
            <button onClick={onOpenGuide} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }} title={tr('帮助', 'Help')}>
                ?
            </button>

            {/* OpenClaw quick entry */}
            <button
                onClick={onOpenConnectorPanel}
                className="btn text-[10px]"
                style={{
                    padding: '3px 8px',
                    flexShrink: 0,
                    borderColor: 'rgba(168,85,247,0.28)',
                    background: 'linear-gradient(135deg, rgba(168,85,247,0.16), rgba(59,130,246,0.08))',
                    color: '#d8b4fe',
                    fontWeight: 700,
                }}
                title={tr('打开 OpenClaw 连接 / 一键接入面板', 'Open OpenClaw connect / quick-connect panel')}
            >
                OpenClaw
            </button>

            {/* ═══ P0-2: OpenClaw Status Bar ═══ */}
            <div
                onClick={onOpenConnectorPanel}
                title={tr('点击打开 OpenClaw 连接面板', 'Click to open the OpenClaw connector panel')}
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 5,
                    padding: '3px 8px',
                    borderRadius: 8,
                    border: '1px solid var(--glass-border)',
                    background: 'rgba(255,255,255,0.02)',
                    cursor: onOpenConnectorPanel ? 'pointer' : 'default',
                    transition: 'background 0.15s, border-color 0.15s',
                    fontSize: 9,
                    color: 'var(--text3)',
                    whiteSpace: 'nowrap',
                    userSelect: 'none',
                    flexShrink: 0,
                }}
                onMouseEnter={e => {
                    e.currentTarget.style.background = 'rgba(255,255,255,0.06)';
                    e.currentTarget.style.borderColor = 'rgba(255,255,255,0.12)';
                }}
                onMouseLeave={e => {
                    e.currentTarget.style.background = 'rgba(255,255,255,0.02)';
                    e.currentTarget.style.borderColor = 'var(--glass-border)';
                }}
            >
                {/* Traffic-light dot with pulse */}
                <span style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    background: statusDotColor,
                    boxShadow: connected ? `0 0 5px ${statusDotColor}` : 'none',
                    animation: connected ? 'ocPulse 2s ease-in-out infinite' : 'none',
                    flexShrink: 0,
                }} />
                <span style={{ fontWeight: 600, color: connected ? '#22c55e' : '#ef4444' }}>
                    {statusLabel}
                </span>
                {instanceBadge && (
                    <span style={{
                        background: '#f59e0b',
                        color: '#000',
                        fontSize: 8,
                        fontWeight: 800,
                        padding: '1px 5px',
                        borderRadius: 4,
                        letterSpacing: 0.5,
                        marginLeft: 2,
                    }}>{instanceBadge}</span>
                )}
                {/* §2.5: OpenClaw Direct Mode badge */}
                {runtimeLabel === 'openclaw' && (
                    <span style={{
                        background: 'linear-gradient(135deg, rgba(168,85,247,0.25), rgba(124,58,237,0.18))',
                        color: '#c084fc',
                        fontSize: 8,
                        fontWeight: 800,
                        padding: '2px 6px',
                        borderRadius: 4,
                        letterSpacing: 0.5,
                        border: '1px solid rgba(168,85,247,0.3)',
                    }}>☁ Direct Mode</span>
                )}
                <span style={{ color: 'var(--text4)' }}>·</span>
                <span style={{ fontSize: 8, color: 'var(--text4)' }}>{wsUrlLabel}</span>
                <span style={{ color: 'var(--text4)' }}>·</span>
                <span>{tr('运行时', 'RT')}: <span style={{ color: runtimeLabel === 'openclaw' ? '#c084fc' : 'var(--text2)', fontWeight: 600 }}>{runtimeLabel}</span></span>
                <span style={{ color: 'var(--text4)' }}>·</span>
                <span>
                    {tr('运行', 'Run')}: <span style={{
                        color: ['running', 'executing', 'queued', 'waiting_review', 'waiting_selfcheck'].includes(String(activeRunStatus || '').trim().toLowerCase())
                            ? '#3b82f6'
                            : (String(activeRunStatus || '').trim().toLowerCase() === 'done' ? '#22c55e'
                            : String(activeRunStatus || '').trim().toLowerCase() === 'failed' ? '#ef4444' : 'var(--text3)'),
                        fontWeight: ['idle', ''].includes(String(activeRunStatus || '').trim().toLowerCase()) ? 400 : 600,
                    }}>{runLabel}</span>
                </span>
            </div>

            {/* Inject pulse keyframes */}
            <style>{`
                @keyframes ocPulse {
                    0%, 100% { opacity: 1; box-shadow: 0 0 6px #22c55e; }
                    50% { opacity: 0.6; box-shadow: 0 0 12px #22c55e; }
                }
            `}</style>
        </div>
    );
}
