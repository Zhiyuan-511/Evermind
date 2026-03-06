'use client';

import { NODE_TYPES } from '@/lib/types';
import { useState } from 'react';

interface SidebarProps {
    onDragStart: (type: string) => void;
    connected: boolean;
    lang: 'en' | 'zh';
}

const CATEGORIES = [
    { key: 'core', label_en: 'Core Agents', label_zh: '核心智能体', types: ['router', 'planner', 'builder', 'tester', 'reviewer'] },
    { key: 'ops', label_en: 'Operations', label_zh: '操作节点', types: ['deployer', 'debugger', 'analyst', 'scribe', 'monitor'] },
    { key: 'tools', label_en: 'Tools', label_zh: '工具节点', types: ['localshell', 'fileread', 'filewrite', 'screenshot', 'browser', 'gitops', 'uicontrol'] },
    { key: 'media', label_en: 'Media', label_zh: '媒体节点', types: ['imagegen', 'bgremove', 'spritesheet', 'assetimport', 'merger'] },
];

export default function Sidebar({ onDragStart, connected, lang }: SidebarProps) {
    const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

    return (
        <aside className="glass-strong flex flex-col h-full" style={{ width: 'var(--sidebar-w)', minWidth: 'var(--sidebar-w)' }}>
            {/* Header */}
            <div className="flex items-center gap-2 px-4 py-3 border-b border-white/5">
                <span className="text-lg">🧠</span>
                <span className="font-bold text-sm">Evermind</span>
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 ml-auto">v2.0</span>
            </div>

            {/* Connection status */}
            <div className="px-4 py-2 border-b border-white/5 flex items-center gap-2 text-[10px]">
                <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-red-400'} ${connected ? 'animate-pulse' : ''}`} />
                <span className="text-[var(--text3)]">{connected ? (lang === 'zh' ? '后端已连接' : 'Backend connected') : (lang === 'zh' ? '离线模式' : 'Offline mode')}</span>
            </div>

            {/* Node palette */}
            <div className="flex-1 overflow-y-auto p-2">
                {CATEGORIES.map(cat => (
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
                                {cat.types.map(t => {
                                    const info = NODE_TYPES[t];
                                    if (!info) return null;
                                    return (
                                        <div
                                            key={t}
                                            className="palette-item"
                                            draggable
                                            onDragStart={() => onDragStart(t)}
                                        >
                                            <span>{info.icon}</span>
                                            <span>{lang === 'zh' ? info.label_zh : info.label_en}</span>
                                            <span className="w-2 h-2 rounded-full ml-auto" style={{ background: info.color }} />
                                        </div>
                                    );
                                })}
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {/* Footer */}
            <div className="px-4 py-2 border-t border-white/5 text-[9px] text-[var(--text3)]">
                {lang === 'zh' ? '拖拽节点到画布' : 'Drag nodes to canvas'}
            </div>
        </aside>
    );
}
