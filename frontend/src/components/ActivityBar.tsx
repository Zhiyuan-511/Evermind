'use client';

/**
 * v7.0 — VSCode/Cursor-style Activity Bar.
 * 48px-wide icon column pinned to the far left. Clicking an icon
 * switches the neighbouring Panel content. Badge numbers show
 * live counts (e.g., dirty file count on Git icon).
 */

export type ActivityView = 'nodes' | 'files' | 'git' | 'reports';

interface ActivityBarProps {
    current: ActivityView;
    onChange: (v: ActivityView) => void;
    lang: 'en' | 'zh';
    gitDirtyCount?: number;
    reportsCount?: number;
}

interface Item {
    id: ActivityView;
    label_en: string;
    label_zh: string;
    icon: React.ReactNode;
}

const ICON = {
    nodes: (
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="6" cy="6" r="2.5" />
            <circle cx="18" cy="6" r="2.5" />
            <circle cx="6" cy="18" r="2.5" />
            <circle cx="18" cy="18" r="2.5" />
            <line x1="8.5" y1="6" x2="15.5" y2="6" />
            <line x1="6" y1="8.5" x2="6" y2="15.5" />
            <line x1="18" y1="8.5" x2="18" y2="15.5" />
            <line x1="8.5" y1="18" x2="15.5" y2="18" />
        </svg>
    ),
    files: (
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 6 H10 l-2-2 H4 a2 2 0 0 0-2 2 v12 a2 2 0 0 0 2 2 h16 a2 2 0 0 0 2-2 V8 a2 2 0 0 0-2-2z" />
        </svg>
    ),
    git: (
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="18" cy="18" r="3" />
            <circle cx="6" cy="6" r="3" />
            <circle cx="6" cy="18" r="3" />
            <path d="M6 9 v6" />
            <path d="M15 18 H9" />
            <path d="M18 15 v-3 a3 3 0 0 0-3-3 H9" />
        </svg>
    ),
    reports: (
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <rect x="4" y="4" width="16" height="16" rx="2" />
            <line x1="8" y1="9" x2="16" y2="9" />
            <line x1="8" y1="13" x2="16" y2="13" />
            <line x1="8" y1="17" x2="12" y2="17" />
        </svg>
    ),
};

const ITEMS: Item[] = [
    { id: 'nodes', label_en: 'Nodes', label_zh: '节点', icon: ICON.nodes },
    { id: 'files', label_en: 'Files', label_zh: '文件', icon: ICON.files },
    { id: 'git', label_en: 'Source Control', label_zh: 'Git 源码', icon: ICON.git },
    { id: 'reports', label_en: 'Reports', label_zh: '报告', icon: ICON.reports },
];

export default function ActivityBar({ current, onChange, lang, gitDirtyCount = 0, reportsCount = 0 }: ActivityBarProps) {
    return (
        <nav
            className="h-full flex flex-col items-center gap-1 border-r border-white/5"
            style={{ width: 48, flexShrink: 0, background: 'rgba(12, 14, 22, 0.85)', paddingTop: 40 /* leave room for macOS traffic lights */ }}
            aria-label={lang === 'zh' ? '活动栏' : 'Activity Bar'}
        >
            {ITEMS.map((it) => {
                const active = current === it.id;
                const badge = it.id === 'git' ? gitDirtyCount : (it.id === 'reports' ? reportsCount : 0);
                return (
                    <button
                        key={it.id}
                        onClick={() => onChange(it.id)}
                        title={lang === 'zh' ? it.label_zh : it.label_en}
                        aria-label={lang === 'zh' ? it.label_zh : it.label_en}
                        aria-pressed={active}
                        style={{
                            position: 'relative',
                            width: 40,
                            height: 40,
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            borderRadius: 8,
                            background: active ? 'rgba(91, 140, 255, 0.18)' : 'transparent',
                            color: active ? 'var(--blue)' : 'var(--text2)',
                            cursor: 'pointer',
                            transition: 'all 0.15s',
                            borderLeft: active ? '2px solid var(--blue)' : '2px solid transparent',
                            marginTop: 2,
                        }}
                        onMouseEnter={(e) => { if (!active) e.currentTarget.style.color = 'var(--text1)'; }}
                        onMouseLeave={(e) => { if (!active) e.currentTarget.style.color = 'var(--text2)'; }}
                    >
                        {it.icon}
                        {badge > 0 && (
                            <span
                                style={{
                                    position: 'absolute',
                                    bottom: 2,
                                    right: 2,
                                    background: it.id === 'git' ? '#f85149' : '#5B8CFF',
                                    color: '#fff',
                                    borderRadius: 10,
                                    minWidth: 16,
                                    height: 14,
                                    padding: '0 3px',
                                    fontSize: 9,
                                    fontWeight: 700,
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    lineHeight: 1,
                                }}
                            >
                                {badge > 99 ? '99+' : badge}
                            </span>
                        )}
                    </button>
                );
            })}
        </nav>
    );
}
