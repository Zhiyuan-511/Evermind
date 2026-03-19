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
    onOpenGuide: () => void;
    onOpenHistory: () => void;
    onOpenDiagnostics: () => void;
    /* Canvas view toggle */
    canvasView: 'editor' | 'preview';
    onToggleCanvasView: () => void;
    hasPreview: boolean;
}

export default function Toolbar({
    workflowName, onNameChange, onRun, onStop, onExport, onClear,
    running, connected, lang, onLangToggle, theme, onThemeToggle,
    onOpenSettings, onOpenTemplates, onOpenGuide,
    onOpenHistory, onOpenDiagnostics,
    canvasView, onToggleCanvasView, hasPreview,
}: ToolbarProps) {
    return (
        <div className="glass-strong flex items-center gap-3 px-4 border-b border-white/5" style={{ height: 'var(--header-h)' }}>
            {/* Workflow name */}
            <input
                value={workflowName}
                onChange={e => onNameChange(e.target.value)}
                className="bg-transparent text-sm font-semibold text-[var(--text1)] border-none outline-none w-48"
                placeholder={lang === 'zh' ? '工作流名称' : 'Workflow name'}
            />

            <div className="h-4 w-px bg-white/10" />

            {/* Run / Stop */}
            {running ? (
                <button onClick={onStop} className="btn btn-danger text-[11px]">
                    ⏹ {lang === 'zh' ? '停止' : 'Stop'}
                </button>
            ) : (
                <button onClick={onRun} className="btn btn-primary text-[11px]">
                    ▶ {lang === 'zh' ? '运行' : 'Run'}
                </button>
            )}

            {/* ═══ Canvas View Toggle ═══ */}
            <div style={{
                display: 'flex', borderRadius: 8,
                border: '1px solid var(--glass-border)',
                overflow: 'hidden',
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
                    🔀 {lang === 'zh' ? '节点' : 'Nodes'}
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
                    🌐 {lang === 'zh' ? '预览' : 'Preview'}
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

            <div className="h-4 w-px bg-white/10" />

            {/* Actions */}
            <button onClick={onExport} className="btn text-[11px]">
                📤 {lang === 'zh' ? '导出' : 'Export'}
            </button>
            <button onClick={onClear} className="btn text-[11px]">
                🗑 {lang === 'zh' ? '清空' : 'Clear'}
            </button>

            <div className="h-4 w-px bg-white/10" />

            {/* Templates */}
            <button onClick={onOpenTemplates} className="btn text-[11px]">
                📂 {lang === 'zh' ? '模板' : 'Templates'}
            </button>

            {/* History */}
            <button onClick={onOpenHistory} className="btn text-[11px]">
                🕘 {lang === 'zh' ? '历史' : 'History'}
            </button>

            {/* Diagnostics */}
            <button onClick={onOpenDiagnostics} className="btn text-[11px]">
                🩺 {lang === 'zh' ? '诊断' : 'Diagnostics'}
            </button>

            {/* Spacer */}
            <div className="flex-1" />

            {/* Theme toggle */}
            <button onClick={onThemeToggle} className="btn text-[11px]">
                {theme === 'dark'
                    ? `☀️ ${lang === 'zh' ? '白天' : 'Light'}`
                    : `🌙 ${lang === 'zh' ? '深夜' : 'Dark'}`}
            </button>

            {/* Lang toggle */}
            <button onClick={onLangToggle} className="btn text-[11px]">
                🌐 {lang === 'zh' ? 'EN' : '中文'}
            </button>

            {/* Settings */}
            <button onClick={onOpenSettings} className="btn text-[11px]" title={lang === 'zh' ? '设置' : 'Settings'}>
                ⚙️
            </button>

            {/* Help */}
            <button onClick={onOpenGuide} className="btn text-[11px]" title={lang === 'zh' ? '帮助' : 'Help'}>
                ❓
            </button>

            {/* Connection */}
            <div className="flex items-center gap-1.5 text-[10px] text-[var(--text3)]">
                <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-red-400'}`} />
                {connected ? 'API ✓' : 'Offline'}
            </div>
        </div>
    );
}
