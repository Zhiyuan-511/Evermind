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
}

export default function Toolbar({
    workflowName, onNameChange, onRun, onStop, onExport, onClear,
    running, connected, lang, onLangToggle, theme, onThemeToggle,
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

            {/* Actions */}
            <button onClick={onExport} className="btn text-[11px]">
                📤 {lang === 'zh' ? '导出' : 'Export'}
            </button>
            <button onClick={onClear} className="btn text-[11px]">
                🗑 {lang === 'zh' ? '清空' : 'Clear'}
            </button>

            {/* Spacer */}
            <div className="flex-1" />

            {/* Theme toggle */}
            <button onClick={onThemeToggle} className="btn text-[11px]">
                {theme === 'dark'
                    ? `☀️ ${lang === 'zh' ? '白天模式' : 'Work mode'}`
                    : `🌙 ${lang === 'zh' ? '深夜模式' : 'Night mode'}`}
            </button>

            {/* Lang toggle */}
            <button onClick={onLangToggle} className="btn text-[11px]">
                🌐 {lang === 'zh' ? 'EN' : '中文'}
            </button>

            {/* Connection */}
            <div className="flex items-center gap-1.5 text-[10px] text-[var(--text3)]">
                <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-red-400'}`} />
                {connected ? 'API ✓' : 'Offline'}
            </div>
        </div>
    );
}
