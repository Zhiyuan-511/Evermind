'use client';

import { normalizeRuntimeModeForDisplay } from '@/lib/runtimeDisplay';

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
    onOpenGitHub?: () => void;
    onOpenTemplates: () => void;
    onOpenSkillsLibrary: () => void;
    onOpenGuide: () => void;
    onOpenHistory: () => void;
    onOpenDiagnostics: () => void;
    /** v5.5 Compound Engineering lessons modal */
    onOpenLessons?: () => void;
    /* Canvas view toggle */
    canvasView: 'editor' | 'preview' | 'files';
    onToggleCanvasView: () => void;
    onSetCanvasView?: (view: 'editor' | 'preview' | 'files') => void;
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
    showOpenClaw?: boolean;
}

export default function Toolbar({
    workflowName, onNameChange, onRun, onStop, onExport, onClear,
    running, connected, lang, onLangToggle, theme, onThemeToggle,
    onOpenSettings, onOpenGitHub, onOpenTemplates, onOpenGuide,
    onOpenSkillsLibrary,
    onOpenHistory, onOpenDiagnostics, onOpenLessons,
    canvasView, onToggleCanvasView, onSetCanvasView, hasPreview,
    activeRunStatus, runtimeModeLabel, activeTaskLabel, activeRunId, lastEventAt, wsUrl, envTag, onOpenConnectorPanel, showOpenClaw = true,
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
    const runtimeLabel = normalizeRuntimeModeForDisplay(runtimeModeLabel || 'local');
    const runtimeLabelText = runtimeLabel ? runtimeLabel.toUpperCase() : 'LOCAL';
    const runLabel = humanizeRunStatus(activeRunStatus || (running ? 'running' : 'idle'));
    const taskLabel = String(activeTaskLabel || '').trim();
    const activeRunStatusKey = String(activeRunStatus || '').trim().toLowerCase();
    const runToneColor = ['running', 'executing', 'queued', 'waiting_review', 'waiting_selfcheck'].includes(activeRunStatusKey)
        ? '#3b82f6'
        : activeRunStatusKey === 'done'
            ? '#22c55e'
            : activeRunStatusKey === 'failed'
                ? '#ef4444'
                : 'var(--text3)';
    // §2.2: Backend URL for diagnostics
    const wsUrlLabel = String(wsUrl || '').replace(/^ws:\/\//, '').replace(/\/ws$/, '') || '127.0.0.1:8765';
    const statusMetaTitle = [
        onOpenConnectorPanel
            ? tr('点击打开连接面板', 'Click to open the connection panel')
            : tr('后端连接状态', 'Backend connection status'),
        `backend: ${wsUrlLabel}`,
        activeRunId ? `run: ${activeRunId}` : '',
        Number.isFinite(Number(lastEventAt)) && Number(lastEventAt) > 0
            ? `last-event: ${new Date(Number(lastEventAt)).toLocaleString()}`
            : '',
    ].filter(Boolean).join(' | ');
    // §2.1: DEV/PACKAGED badge
    const instanceBadge = envTag === 'dev' ? 'DEV' : envTag === 'packaged' ? '' : '';

    return (
        <div className="glass-strong flex items-center px-3 border-b border-white/5" style={{ height: 'var(--header-h)', overflow: 'hidden', minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0, overflowX: 'auto', overflowY: 'hidden', paddingRight: 8 }}>
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
                        onClick={() => { if (canvasView !== 'editor') { onSetCanvasView ? onSetCanvasView('editor') : onToggleCanvasView(); } }}
                        style={{
                            padding: '4px 10px', fontSize: 10, fontWeight: 600,
                            border: 'none', cursor: canvasView === 'editor' ? 'default' : 'pointer',
                            background: canvasView === 'editor' ? 'rgba(91,140,255,0.15)' : 'transparent',
                            color: canvasView === 'editor' ? 'var(--blue)' : 'var(--text3)',
                            transition: 'all 0.15s',
                        }}
                    >
                        {tr('节点', 'Nodes')}
                    </button>
                    <button
                        onClick={() => { if (canvasView !== 'preview') { onSetCanvasView ? onSetCanvasView('preview') : onToggleCanvasView(); } }}
                        style={{
                            padding: '4px 10px', fontSize: 10, fontWeight: 600,
                            border: 'none', cursor: canvasView === 'preview' ? 'default' : 'pointer',
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
                    <button
                        onClick={() => { if (canvasView !== 'files' && onSetCanvasView) onSetCanvasView('files'); }}
                        style={{
                            padding: '4px 10px', fontSize: 10, fontWeight: 600,
                            border: 'none', cursor: 'pointer',
                            background: canvasView === 'files' ? 'rgba(168,85,247,0.15)' : 'transparent',
                            color: canvasView === 'files' ? '#a855f7' : 'var(--text3)',
                            transition: 'all 0.15s',
                        }}
                    >
                        {tr('文件', 'Files')}
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
                {onOpenLessons && (
                    <button
                        onClick={onOpenLessons}
                        className="btn text-[10px]"
                        style={{ padding: '3px 8px', flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4 }}
                        title={tr('查看 Evermind 从过往任务中学到的教训', 'Lessons Evermind has learned from past runs')}
                    >
                        <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M8 2a4.5 4.5 0 0 0-2.5 8.25V11a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-.75A4.5 4.5 0 0 0 8 2Z"/>
                            <path d="M6 13.5h4M6.5 15h3"/>
                        </svg>
                        {tr('经验', 'Lessons')}
                    </button>
                )}
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0, minWidth: 0, paddingLeft: 8, borderLeft: '1px solid rgba(255,255,255,0.06)' }}>
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

                {/* GitHub (v6.4.5) */}
                {onOpenGitHub && (
                    <button
                        onClick={onOpenGitHub}
                        className="btn text-[10px]"
                        style={{
                            padding: '3px 10px',
                            flexShrink: 0,
                            background: 'linear-gradient(135deg,#0071e3 0%,#af52de 100%)',
                            color: '#fff',
                            border: 'none',
                            fontWeight: 600,
                        }}
                        title={tr('发布到 GitHub', 'Publish to GitHub')}
                    >
                        {tr('GitHub', 'GitHub')}
                    </button>
                )}

                {/* Help */}
                <button onClick={onOpenGuide} className="btn text-[10px]" style={{ padding: '3px 6px', flexShrink: 0 }} title={tr('帮助', 'Help')}>
                    ?
                </button>

                {showOpenClaw && (
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
                )}

                <div
                    onClick={onOpenConnectorPanel}
                    title={statusMetaTitle}
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
                        maxWidth: 340,
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
                <span style={{ fontSize: 8, color: 'var(--text4)', maxWidth: 108, overflow: 'hidden', textOverflow: 'ellipsis' }}>{wsUrlLabel}</span>
                <span style={{ color: 'var(--text4)' }}>·</span>
                <span>
                    {tr('运行时', 'RT')}: <span style={{ color: runtimeLabel === 'openclaw' ? '#c084fc' : 'var(--text2)', fontWeight: 600 }}>{runtimeLabelText}</span>
                </span>
                <span style={{ color: 'var(--text4)' }}>·</span>
                <span>
                    {tr('运行', 'Run')}: <span style={{
                        color: runToneColor,
                        fontWeight: ['idle', ''].includes(activeRunStatusKey) ? 400 : 600,
                    }}>{runLabel}</span>
                </span>
                </div>
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
