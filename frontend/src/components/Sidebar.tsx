'use client';

import { NODE_TYPES } from '@/lib/types';
import { useEffect, useState } from 'react';
import FileExplorerPanel from './FileExplorerPanel';
import ActivityBar, { ActivityView } from './ActivityBar';
import GitDiffPanel from './GitDiffPanel';
import ReportsPanel from './ReportsPanel';

interface SidebarProps {
    onDragStart: (type: string) => void;
    connected: boolean;
    lang: 'en' | 'zh';
    onOpenArtifacts?: () => void;
    onOpenReports?: () => void;
    onOpenSkillsLibrary?: () => void;
    onOpenFile?: (filePath: string, root: string, content: string, ext: string) => void;
    forcedMode?: 'nodes' | 'files';
}

// v5.8.6: added router (entry) + merger (required for multi-builder DAGs).
const CATEGORIES = [
    { key: 'core', label_en: 'AI Agents', label_zh: 'AI 智能体', types: ['router', 'planner', 'analyst', 'uidesign', 'builder', 'merger', 'polisher', 'patcher', 'reviewer', 'tester', 'debugger', 'deployer', 'scribe'] },
    { key: 'media', label_en: 'Art & Media', label_zh: '美术 & 媒体', types: ['imagegen', 'spritesheet', 'assetimport'] },
];

/**
 * v7.0 (maintainer 2026-04-24): VSCode/Cursor-style 2-column layout.
 * Column A  (48 px) = Activity Bar — Nodes / Files / Git / Reports icons.
 * Column B (240 px) = Panel content for the currently selected activity.
 * A fold toggle still collapses both columns for canvas-only mode.
 */
export default function Sidebar({ onDragStart, connected, lang, onOpenReports, onOpenSkillsLibrary, onOpenFile, forcedMode }: SidebarProps) {
    const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
    const [search, setSearch] = useState('');
    const [sidebarOpen, setSidebarOpen] = useState(true);
    const [currentView, setCurrentView] = useState<ActivityView>('nodes');
    const [gitDirtyCount, setGitDirtyCount] = useState(0);
    const [reportsCount, setReportsCount] = useState(0);

    // Honour explicit forced mode from parent (e.g. canvasView === 'files').
    useEffect(() => {
        if (forcedMode === 'files') setCurrentView('files');
        else if (forcedMode === 'nodes') setCurrentView('nodes');
    }, [forcedMode]);

    const matchesSearch = (type: string) => {
        if (!search) return true;
        const q = search.toLowerCase();
        const info = NODE_TYPES[type];
        if (!info) return false;
        return type.includes(q) || info.label_en.toLowerCase().includes(q) || info.label_zh.includes(q) || info.desc_en.toLowerCase().includes(q) || info.desc_zh.includes(q);
    };

    return (
        <>
            <button
                className="sidebar-toggle"
                onClick={() => setSidebarOpen(prev => !prev)}
                title={sidebarOpen ? (lang === 'zh' ? '收起面板' : 'Collapse') : (lang === 'zh' ? '展开面板' : 'Expand')}
            >
                {sidebarOpen ? '◀' : '▶'}
            </button>

            {sidebarOpen && (
                <div className="flex h-full" style={{ flexShrink: 0 }}>
                    {/* Column A: Activity Bar */}
                    <ActivityBar
                        current={currentView}
                        onChange={setCurrentView}
                        lang={lang}
                        gitDirtyCount={gitDirtyCount}
                        reportsCount={reportsCount}
                    />

                    {/* Column B: dynamic panel */}
                    <aside
                        className="glass-strong flex flex-col h-full sidebar-panel"
                        style={{ width: 240, flexShrink: 0 }}
                    >
                        {/* macOS traffic light spacer */}
                        <div
                            className="flex-shrink-0"
                            style={{ height: 36 }}
                            ref={(el) => { if (el) el.style.setProperty('-webkit-app-region', 'drag'); }}
                        />

                        {/* Header with brand + connection status (shared across panels) */}
                        <div
                            className="flex items-center gap-2 px-4 pb-2 border-b border-white/5"
                            ref={(el) => { if (el) el.style.setProperty('-webkit-app-region', 'drag'); }}
                        >
                            <span style={{
                                width: 20, height: 20, borderRadius: 6,
                                background: 'linear-gradient(135deg, #5B8CFF, #6c5ce7)',
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                                fontSize: 11, fontWeight: 800, color: '#fff',
                            }}>E</span>
                            <span className="font-bold text-sm">Evermind</span>
                            <span className="text-[9px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 ml-auto">v7.0</span>
                        </div>
                        <div className="px-4 py-1.5 border-b border-white/5 flex items-center gap-2 text-[10px]">
                            <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-red-400'} ${connected ? 'animate-pulse' : ''}`} />
                            <span className="text-[var(--text3)]">{connected ? (lang === 'zh' ? '后端已连接' : 'Backend connected') : (lang === 'zh' ? '离线模式' : 'Offline mode')}</span>
                        </div>

                        {/* Panel content */}
                        {currentView === 'files' && (
                            <div className="flex-1 overflow-hidden">
                                <FileExplorerPanel lang={lang} onOpenFile={onOpenFile} />
                            </div>
                        )}

                        {currentView === 'git' && (
                            <GitDiffPanel lang={lang} onDirtyCountChange={setGitDirtyCount} />
                        )}

                        {currentView === 'reports' && (
                            <ReportsPanel lang={lang} onOpenFullReports={onOpenReports} onCountChange={setReportsCount} />
                        )}

                        {currentView === 'nodes' && (
                            <>
                                {/* Quick Actions */}
                                <div className="px-3 py-2 border-b border-white/5 grid" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 6 }}>
                                    <button
                                        className="btn text-[10px]"
                                        onClick={onOpenReports}
                                        title={lang === 'zh' ? '查看执行报告' : 'Open reports'}
                                        style={{
                                            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4,
                                            padding: '6px 8px', borderRadius: 8,
                                            background: 'rgba(0,206,201,0.08)', border: '1px solid rgba(0,206,201,0.2)',
                                        }}
                                    >
                                        <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#00cec9' }} /> {lang === 'zh' ? '报告' : 'Reports'}
                                    </button>
                                    <button
                                        className="btn text-[10px]"
                                        onClick={onOpenSkillsLibrary}
                                        title={lang === 'zh' ? '打开技能库 / 资源库' : 'Open skills library'}
                                        style={{
                                            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4,
                                            padding: '6px 8px', borderRadius: 8,
                                            background: 'rgba(91,140,255,0.08)', border: '1px solid rgba(91,140,255,0.2)',
                                        }}
                                    >
                                        <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#5B8CFF' }} /> {lang === 'zh' ? '技能库' : 'Skills'}
                                    </button>
                                </div>

                                <div className="px-3 py-2 border-b border-white/5">
                                    <input
                                        value={search}
                                        onChange={e => setSearch(e.target.value)}
                                        placeholder={lang === 'zh' ? '搜索节点...' : 'Search nodes...'}
                                        className="w-full bg-white/5 border border-white/10 rounded-md px-3 py-1.5 text-[10px] text-[var(--text1)] placeholder:text-[var(--text3)] focus:outline-none focus:border-[var(--blue)] transition-colors"
                                    />
                                </div>

                                <div className="flex-1 overflow-y-auto p-2">
                                    {CATEGORIES.map(cat => {
                                        const visibleTypes = cat.types.filter(matchesSearch);
                                        if (visibleTypes.length === 0) return null;
                                        return (
                                            <div key={cat.key} className="mb-2">
                                                <button
                                                    className="w-full text-left px-2 py-1.5 text-[10px] font-bold text-[var(--text3)] uppercase tracking-wider flex items-center justify-between hover:text-[var(--text2)] transition-colors"
                                                    onClick={() => setCollapsed(prev => ({ ...prev, [cat.key]: !prev[cat.key] }))}
                                                >
                                                    {lang === 'zh' ? cat.label_zh : cat.label_en}
                                                    <span className="text-[8px]">{collapsed[cat.key] ? '▸' : '▾'}</span>
                                                </button>

                                                {!collapsed[cat.key] && (
                                                    <div className="space-y-0.5">
                                                        {visibleTypes.map(t => {
                                                            const info = NODE_TYPES[t];
                                                            if (!info) return null;
                                                            return (
                                                                <div
                                                                    key={t}
                                                                    className="palette-item"
                                                                    draggable
                                                                    onDragStart={() => onDragStart(t)}
                                                                >
                                                                    <div className="flex items-center justify-center w-6 h-6 rounded-md flex-shrink-0" style={{ background: `linear-gradient(135deg, ${info.color}40, ${info.color}20)` }}>
                                                                        <span style={{ fontSize: 12 }}>{info.icon}</span>
                                                                    </div>
                                                                    <div className="min-w-0 flex-1">
                                                                        <div className="text-[10px] font-semibold leading-tight">{lang === 'zh' ? info.label_zh : info.label_en}</div>
                                                                        <div className="text-[7px] text-[var(--text3)] truncate">{lang === 'zh' ? info.desc_zh : info.desc_en}</div>
                                                                    </div>
                                                                    {info.sec && (
                                                                        <span className="text-[7px] font-bold px-1 rounded" style={{
                                                                            background: info.sec === 'L1' ? 'rgba(88,166,255,0.15)' : info.sec === 'L2' ? 'rgba(63,185,80,0.15)' : 'rgba(210,153,34,0.15)',
                                                                            color: info.sec === 'L1' ? '#58a6ff' : info.sec === 'L2' ? '#3fb950' : '#d29922',
                                                                        }}>{info.sec}</span>
                                                                    )}
                                                                    <span className="w-2 h-2 rounded-full ml-auto flex-shrink-0" style={{ background: info.color }} />
                                                                </div>
                                                            );
                                                        })}
                                                    </div>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>

                                <div className="px-3 py-2 border-t border-white/5">
                                    <div className="text-[9px] text-[var(--text3)] px-1">
                                        {lang === 'zh' ? '拖拽节点到画布' : 'Drag nodes to canvas'}
                                    </div>
                                </div>
                            </>
                        )}
                    </aside>
                </div>
            )}
        </>
    );
}
