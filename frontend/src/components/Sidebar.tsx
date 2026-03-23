'use client';

import { NODE_TYPES } from '@/lib/types';
import { useState } from 'react';

interface SidebarProps {
    onDragStart: (type: string) => void;
    connected: boolean;
    lang: 'en' | 'zh';
    onOpenArtifacts?: () => void;
    onOpenReports?: () => void;
    onOpenSkillsLibrary?: () => void;

}

const CATEGORIES = [
    { key: 'core', label_en: 'AI Agents', label_zh: 'AI 智能体', types: ['router', 'planner', 'builder', 'tester', 'reviewer', 'deployer', 'debugger', 'analyst', 'scribe'] },
    { key: 'tools', label_en: 'Local Execution', label_zh: '本地执行', types: ['localshell', 'fileread', 'filewrite', 'screenshot', 'browser', 'gitops', 'uicontrol'] },
    { key: 'media', label_en: 'Art & Media', label_zh: '美术 & 媒体', types: ['imagegen', 'bgremove', 'spritesheet', 'assetimport', 'merger'] },
];

export default function Sidebar({ onDragStart, connected, lang, onOpenArtifacts, onOpenReports, onOpenSkillsLibrary }: SidebarProps) {
    const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
    const [search, setSearch] = useState('');
    const [sidebarOpen, setSidebarOpen] = useState(true);

    const matchesSearch = (type: string) => {
        if (!search) return true;
        const q = search.toLowerCase();
        const info = NODE_TYPES[type];
        if (!info) return false;
        return type.includes(q) || info.label_en.toLowerCase().includes(q) || info.label_zh.includes(q) || info.desc_en.toLowerCase().includes(q) || info.desc_zh.includes(q);
    };

    return (
        <>
            {/* Collapse/Expand toggle */}
            <button
                className="sidebar-toggle"
                onClick={() => setSidebarOpen(prev => !prev)}
                title={sidebarOpen ? (lang === 'zh' ? '收起面板' : 'Collapse') : (lang === 'zh' ? '展开面板' : 'Expand')}
            >
                {sidebarOpen ? '◀' : '▶'}
            </button>

            {sidebarOpen && (
                <aside className="glass-strong flex flex-col h-full sidebar-panel">
                    {/* macOS traffic light spacer — keeps content below ●●● buttons */}
                    <div
                        className="flex-shrink-0"
                        style={{
                            height: 36,
                            // @ts-expect-error webkit vendor property
                            WebkitAppRegionX: 'drag',
                        }}
                        // Also use the standard CSS property via inline
                        ref={(el) => { if (el) el.style.setProperty('-webkit-app-region', 'drag'); }}
                    />

                    {/* Header */}
                    <div
                        className="flex items-center gap-2 px-4 pb-3 border-b border-white/5"
                        style={{ WebkitAppRegion: 'drag' } as React.CSSProperties}
                        ref={(el) => { if (el) el.style.setProperty('-webkit-app-region', 'drag'); }}
                    >
                        <span style={{
                            width: 20, height: 20, borderRadius: 6,
                            background: 'linear-gradient(135deg, #4f8fff, #6c5ce7)',
                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                            fontSize: 11, fontWeight: 800, color: '#fff',
                        }}>E</span>
                        <span className="font-bold text-sm">Evermind</span>
                        <span className="text-[9px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 ml-auto">v2.0</span>
                    </div>

                    {/* Connection status */}
                    <div className="px-4 py-2 border-b border-white/5 flex items-center gap-2 text-[10px]">
                        <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-red-400'} ${connected ? 'animate-pulse' : ''}`} />
                        <span className="text-[var(--text3)]">{connected ? (lang === 'zh' ? '后端已连接' : 'Backend connected') : (lang === 'zh' ? '离线模式' : 'Offline mode')}</span>
                    </div>

                    {/* Quick Actions: Files / Reports / Skills */}
                    <div className="px-3 py-2.5 border-b border-white/5 grid" style={{ gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 8 }}>
                        <button
                            className="btn text-[10px] flex-1"
                            onClick={onOpenArtifacts}
                            title={lang === 'zh' ? '查看生成文件' : 'Open artifacts'}
                            style={{
                                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 5,
                                padding: '7px 10px', borderRadius: 8,
                                background: 'rgba(108,92,231,0.08)', border: '1px solid rgba(108,92,231,0.2)',
                            }}
                        >
                            <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#6c5ce7', flexShrink: 0 }} /> {lang === 'zh' ? '文件' : 'Files'}
                        </button>
                        <button
                            className="btn text-[10px] flex-1"
                            onClick={onOpenReports}
                            title={lang === 'zh' ? '查看执行报告' : 'Open reports'}
                            style={{
                                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 5,
                                padding: '7px 10px', borderRadius: 8,
                                background: 'rgba(0,206,201,0.08)', border: '1px solid rgba(0,206,201,0.2)',
                            }}
                        >
                            <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#00cec9', flexShrink: 0 }} /> {lang === 'zh' ? '报告' : 'Reports'}
                        </button>
                        <button
                            className="btn text-[10px] flex-1"
                            onClick={onOpenSkillsLibrary}
                            title={lang === 'zh' ? '打开技能库 / 资源库' : 'Open skills library'}
                            style={{
                                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 5,
                                padding: '7px 10px', borderRadius: 8,
                                background: 'rgba(79,143,255,0.08)', border: '1px solid rgba(79,143,255,0.2)',
                            }}
                        >
                            <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#4f8fff', flexShrink: 0 }} /> {lang === 'zh' ? '技能库' : 'Skills'}
                        </button>
                    </div>

                    {/* Search */}
                    <div className="px-3 py-2 border-b border-white/5">
                        <input
                            value={search}
                            onChange={e => setSearch(e.target.value)}
                            placeholder={lang === 'zh' ? '搜索节点...' : 'Search nodes...'}
                            className="w-full bg-white/5 border border-white/10 rounded-md px-3 py-1.5 text-[10px] text-[var(--text1)] placeholder:text-[var(--text3)] focus:outline-none focus:border-[var(--blue)] transition-colors"
                        />
                    </div>

                    {/* Node palette */}
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

                    {/* Footer */}
                    <div className="px-3 py-2 border-t border-white/5">
                        <div className="text-[9px] text-[var(--text3)] px-1">
                            {lang === 'zh' ? '拖拽节点到画布' : 'Drag nodes to canvas'}
                        </div>
                    </div>
                </aside>
            )}
        </>
    );
}
