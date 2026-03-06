'use client';

import React, { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { NODE_TYPES } from '@/lib/types';

function AgentNode({ data, selected }: NodeProps) {
    const nodeType = data.nodeType as string || 'builder';
    const info = NODE_TYPES[nodeType] || { icon: '❓', color: '#666', label_en: nodeType, label_zh: nodeType };
    const status = data.status as string || 'idle';
    const progress = (data.progress as number) || 0;
    const model = (data.model as string) || 'gpt-5.4';
    const lastOutput = data.lastOutput as string || '';
    const lang = (data.lang as string) || 'en';
    const label = lang === 'zh' ? info.label_zh : info.label_en;
    const name = (data.label as string) || label;
    const c = info.color;

    const isRunning = status === 'running';
    const isDone = status === 'done';
    const isError = status === 'error';

    return (
        <div style={{
            width: 180,
            borderRadius: 10,
            overflow: 'hidden',
            background: `linear-gradient(160deg, ${c}10, rgba(14,14,32,0.92) 50%, rgba(10,10,26,0.96))`,
            backdropFilter: 'blur(16px)',
            border: `1px solid ${selected ? c + '70' : 'rgba(255,255,255,0.07)'}`,
            boxShadow: selected
                ? `0 0 0 1.5px ${c}50, 0 6px 20px rgba(0,0,0,0.5)`
                : isRunning
                    ? `0 0 16px ${c}25, 0 4px 16px rgba(0,0,0,0.4)`
                    : `0 3px 16px rgba(0,0,0,0.35)`,
            transition: 'all 0.2s ease',
            fontSize: 0, /* reset inline spacing */
        }}>
            {/* Input Handle */}
            <Handle type="target" position={Position.Left} style={{
                width: 10, height: 10, border: `2px solid ${c}70`,
                background: '#0e0e20', left: -5, top: '50%',
            }} />

            {/* Header — compact */}
            <div style={{
                padding: '6px 10px 5px',
                display: 'flex', alignItems: 'center', gap: 6,
                background: `linear-gradient(135deg, ${c}20, transparent)`,
                borderBottom: `1px solid ${c}18`,
            }}>
                <span style={{ fontSize: 13, lineHeight: 1 }}>{info.icon}</span>
                <span style={{
                    fontSize: 11, fontWeight: 600, color: '#e0e0f0',
                    flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{name}</span>
                <span style={{
                    width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                    background: isRunning ? '#4f8fff' : isDone ? '#40d67c' : isError ? '#ff4f6a' : '#2a2a4a',
                    boxShadow: isRunning ? '0 0 6px #4f8fff' : isDone ? '0 0 4px #40d67c' : 'none',
                    animation: isRunning ? 'agentPulse 1.5s infinite' : 'none',
                }} />
            </div>

            {/* Body — compact */}
            <div style={{ padding: '5px 10px 7px' }}>
                <div style={{
                    fontSize: 9, color: '#5a5a7a', display: 'flex', gap: 3, alignItems: 'center',
                }}>
                    <span style={{
                        padding: '1px 5px', borderRadius: 3, fontSize: 8,
                        background: 'rgba(79,143,255,0.08)', color: '#5a8ad4',
                        border: '1px solid rgba(79,143,255,0.12)',
                    }}>{model}</span>
                    <span style={{
                        padding: '1px 5px', borderRadius: 3, fontSize: 8,
                        background: `${c}0c`, color: c + 'bb',
                        border: `1px solid ${c}15`,
                    }}>{nodeType}</span>
                </div>

                {/* Progress */}
                {progress > 0 && isRunning && (
                    <div style={{
                        marginTop: 5, height: 3, borderRadius: 2,
                        background: 'rgba(255,255,255,0.04)', overflow: 'hidden',
                    }}>
                        <div style={{
                            height: '100%', borderRadius: 2, width: `${progress}%`,
                            background: `linear-gradient(90deg, ${c}, #a855f7)`,
                            transition: 'width 0.3s',
                        }} />
                    </div>
                )}

                {/* Output preview */}
                {lastOutput && (
                    <div style={{
                        marginTop: 4, fontSize: 8, color: '#6a6a8a', lineHeight: 1.3,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>{lastOutput.substring(0, 60)}...</div>
                )}

                {/* Status text for done/error */}
                {isDone && <div style={{ marginTop: 4, fontSize: 8, color: '#40d67c' }}>✓ Complete</div>}
                {isError && <div style={{ marginTop: 4, fontSize: 8, color: '#ff4f6a' }}>✗ Error</div>}
            </div>

            {/* Output Handle */}
            <Handle type="source" position={Position.Right} style={{
                width: 10, height: 10, border: `2px solid ${c}70`,
                background: '#0e0e20', right: -5, top: '50%',
            }} />

            <style>{`
        @keyframes agentPulse { 0%,100% { opacity:1 } 50% { opacity:0.3 } }
      `}</style>
        </div>
    );
}

export default memo(AgentNode);
