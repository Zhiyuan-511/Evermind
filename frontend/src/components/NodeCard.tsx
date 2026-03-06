'use client';

import { NodeData, NODE_TYPES } from '@/lib/types';

interface NodeCardProps {
    node: NodeData;
    selected: boolean;
    onSelect: (id: string) => void;
    onDragStart: (e: React.MouseEvent, id: string) => void;
    lang: 'en' | 'zh';
}

export default function NodeCard({ node, selected, onSelect, onDragStart, lang }: NodeCardProps) {
    const info = NODE_TYPES[node.type] || { icon: '❓', color: '#666', label_en: node.type, label_zh: node.type };
    const statusColors: Record<string, string> = {
        idle: 'transparent',
        running: 'var(--blue)',
        done: 'var(--green)',
        error: 'var(--red)',
    };

    return (
        <div
            className={`node-card glass ${selected ? 'selected' : ''} ${node.status === 'running' ? 'running' : ''}`}
            style={{ left: node.x, top: node.y }}
            onMouseDown={(e) => {
                e.stopPropagation();
                onSelect(node.id);
                onDragStart(e, node.id);
            }}
        >
            {/* Header */}
            <div className="node-header" style={{ background: info.color + '30', borderBottom: `2px solid ${info.color}40` }}>
                <span>{info.icon}</span>
                <span className="flex-1 truncate">{node.name || (lang === 'zh' ? info.label_zh : info.label_en)}</span>
                <span
                    className="w-2 h-2 rounded-full"
                    style={{ background: statusColors[node.status] || 'transparent', boxShadow: node.status !== 'idle' ? `0 0 6px ${statusColors[node.status]}` : 'none' }}
                />
            </div>

            {/* Body */}
            <div className="node-body">
                <div className="truncate text-[9px] text-[var(--text3)]">
                    {node.model || 'gpt-5.4'} • {node.type}
                </div>
                {node.progress > 0 && node.status === 'running' && (
                    <div className="progress-bar mt-1.5">
                        <div className="fill" style={{ width: `${node.progress}%` }} />
                    </div>
                )}
                {node.lastOutput && (
                    <div className="mt-1.5 text-[8px] text-[var(--text2)] line-clamp-2">
                        {node.lastOutput.substring(0, 80)}...
                    </div>
                )}
            </div>

            {/* Ports */}
            {node.inputs.map((port, i) => (
                <div
                    key={port.id}
                    className="node-port input"
                    style={{ top: 30 + i * 20 }}
                    title={port.label}
                    data-port-id={port.id}
                    data-port-type="input"
                />
            ))}
            {node.outputs.map((port, i) => (
                <div
                    key={port.id}
                    className="node-port output"
                    style={{ top: 30 + i * 20 }}
                    title={port.label}
                    data-port-id={port.id}
                    data-port-type="output"
                />
            ))}
        </div>
    );
}
