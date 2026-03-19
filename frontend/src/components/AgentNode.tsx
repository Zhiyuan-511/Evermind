'use client';

import React, { memo, useState, useCallback, useRef, useEffect } from 'react';
import { Handle, Position, useReactFlow, type NodeProps } from '@xyflow/react';
import { NODE_TYPES } from '@/lib/types';
import type { CanvasNodeStatus } from '@/lib/types';

// ── V1 Status Config ──
const STATUS_CONFIG: Record<CanvasNodeStatus, {
    color: string; glow: string; dot: string;
    label_en: string; label_zh: string;
    pulse?: boolean;
}> = {
    queued:            { color: '#8b8fa3', glow: 'none', dot: '#8b8fa3', label_en: 'Queued', label_zh: '排队中' },
    running:           { color: '#4f8fff', glow: '0 0 14px #4f8fff30', dot: '#4f8fff', label_en: 'Running', label_zh: '执行中', pulse: true },
    passed:            { color: '#40d67c', glow: '0 0 8px #40d67c20', dot: '#40d67c', label_en: 'Passed', label_zh: '已完成' },
    failed:            { color: '#ff4f6a', glow: '0 0 10px #ff4f6a25', dot: '#ff4f6a', label_en: 'Failed', label_zh: '失败' },
    blocked:           { color: '#ff9b47', glow: '0 0 8px #ff9b4720', dot: '#ff9b47', label_en: 'Blocked', label_zh: '阻塞' },
    waiting_approval:  { color: '#f59e0b', glow: '0 0 12px #f59e0b25', dot: '#f59e0b', label_en: 'Awaiting Review', label_zh: '待审核', pulse: true },
    skipped:           { color: '#666', glow: 'none', dot: '#555', label_en: 'Skipped', label_zh: '已跳过' },
    idle:              { color: '#555', glow: 'none', dot: 'var(--node-dot-idle)', label_en: '', label_zh: '' },
    done:              { color: '#40d67c', glow: '0 0 8px #40d67c20', dot: '#40d67c', label_en: 'Complete', label_zh: '已完成' },
    error:             { color: '#ff4f6a', glow: '0 0 10px #ff4f6a25', dot: '#ff4f6a', label_en: 'Error', label_zh: '错误' },
};

function getStatusConfig(status: string) {
    return STATUS_CONFIG[(status as CanvasNodeStatus)] || STATUS_CONFIG.idle;
}

function getNodeMark(label: string, fallback: string): string {
    const source = (label || fallback || 'node').trim();
    const compact = source.replace(/\s+/g, ' ').trim();
    const han = compact.match(/[\p{Script=Han}A-Za-z0-9]/gu) || [];
    if (han.length >= 2) return `${han[0]}${han[1]}`.toUpperCase();
    if (han.length === 1) return han[0].toUpperCase();
    const words = compact.split(/[\s/_-]+/).filter(Boolean);
    if (words.length >= 2) return `${words[0][0]}${words[1][0]}`.toUpperCase();
    return compact.slice(0, 2).toUpperCase();
}

// Format helpers
function formatTokens(n: number): string {
    if (!n) return '';
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
    return String(n);
}

function formatCost(n: number): string {
    if (!n) return '';
    if (n < 0.01) return `$${n.toFixed(4)}`;
    return `$${n.toFixed(2)}`;
}

function formatDuration(startMs: number, endMs: number): string {
    if (!startMs) return '';
    const end = endMs || Date.now();
    const sec = Math.round((end - startMs) / 1000);
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
    return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

// Preset model list for per-node selection
const AVAILABLE_MODELS = [
    { id: 'gpt-5.4', label: 'GPT-5.4', provider: 'OpenAI' },
    { id: 'gpt-4o', label: 'GPT-4o', provider: 'OpenAI' },
    { id: 'gpt-4o-mini', label: 'GPT-4o Mini', provider: 'OpenAI' },
    { id: 'claude-4-sonnet', label: 'Claude 4 Sonnet', provider: 'Anthropic' },
    { id: 'claude-3.5-sonnet', label: 'Claude 3.5 Sonnet', provider: 'Anthropic' },
    { id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro', provider: 'Google' },
    { id: 'gemini-2.0-flash', label: 'Gemini 2.0 Flash', provider: 'Google' },
    { id: 'kimi', label: 'Kimi', provider: 'Moonshot' },
    { id: 'kimi-k2.5', label: 'Kimi K2.5', provider: 'Moonshot' },
    { id: 'kimi-coding', label: 'Kimi Coding', provider: 'Moonshot' },
    { id: 'deepseek-r1', label: 'DeepSeek R1', provider: 'DeepSeek' },
    { id: 'deepseek-v3', label: 'DeepSeek V3', provider: 'DeepSeek' },
    { id: 'qwen-max', label: 'Qwen Max', provider: 'Alibaba' },
    { id: 'qwen-plus', label: 'Qwen Plus', provider: 'Alibaba' },
    { id: 'doubao-pro', label: '豆包 Pro', provider: 'ByteDance' },
    { id: 'glm-4', label: 'GLM-4', provider: 'Zhipu' },
];

function AgentNode({ id, data, selected }: NodeProps) {
    const nodeType = data.nodeType as string || 'builder';
    const info = NODE_TYPES[nodeType] || { icon: '', color: '#666', label_en: nodeType, label_zh: nodeType, desc_en: '', desc_zh: '', inputs: [{ id: 'in', label: 'Input' }], outputs: [{ id: 'out', label: 'Output' }] };
    const rawStatus = data.status as string || 'idle';
    const progress = (data.progress as number) || 0;
    const model = (data.model as string) || '';
    const assignedModel = (data.assignedModel as string) || '';
    const displayModel = assignedModel || model || 'gpt-5.4';
    const lastOutput = data.lastOutput as string || '';
    const outputSummary = data.outputSummary as string || '';
    const displayOutput = outputSummary || lastOutput;
    const tokensUsed = data.tokensUsed as number || 0;
    const cost = data.cost as number || 0;
    const startedAt = data.startedAt as number || 0;
    const endedAt = data.endedAt as number || 0;
    const lang = (data.lang as string) || 'en';
    const label = lang === 'zh' ? info.label_zh : info.label_en;
    const desc = lang === 'zh' ? info.desc_zh : info.desc_en;
    const name = (data.label as string) || label;
    const c = info.color;
    const nodeMark = getNodeMark(name, nodeType);

    // Get V1 status config
    const sc = getStatusConfig(rawStatus);
    const isRunning = rawStatus === 'running';
    const hasMetrics = tokensUsed > 0 || cost > 0 || startedAt > 0;

    // Model selector state
    const [modelOpen, setModelOpen] = useState(false);
    const dropdownRef = useRef<HTMLDivElement>(null);
    const { setNodes } = useReactFlow();

    const handleModelChange = useCallback((newModel: string) => {
        setNodes(nds => nds.map(n =>
            n.id === id ? { ...n, data: { ...n.data, model: newModel } } : n
        ));
        setModelOpen(false);
    }, [id, setNodes]);

    // Close dropdown when clicking outside
    useEffect(() => {
        if (!modelOpen) return;
        const handleClickOutside = (e: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(e.target as HTMLElement)) {
                setModelOpen(false);
            }
        };
        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [modelOpen]);

    // Expandable output state
    const [outputExpanded, setOutputExpanded] = useState(false);

    // Live timer for running nodes
    const [, setTick] = useState(0);
    useEffect(() => {
        if (!isRunning || !startedAt) return;
        const timer = setInterval(() => setTick(t => t + 1), 1000);
        return () => clearInterval(timer);
    }, [isRunning, startedAt]);

    return (
        <div className="agent-node-card" style={{
            '--node-accent': c,
            width: 220,
            borderRadius: 12,
            overflow: 'visible',
            border: `1.5px solid ${selected ? c + '60' : sc.color + '25'}`,
            boxShadow: selected
                ? `0 0 0 1.5px ${c}40, var(--node-shadow)`
                : `${sc.glow}, var(--node-shadow)`,
            transition: 'all 0.25s ease',
            fontSize: 0,
            position: 'relative',
            background: 'var(--node-bg)',
        } as React.CSSProperties}>

            {/* ── Header ── */}
            <div style={{
                padding: '7px 10px 5px',
                display: 'flex', alignItems: 'center', gap: 6,
                background: `linear-gradient(135deg, ${c}18, transparent)`,
                borderBottom: `1px solid var(--node-divider)`,
            }}>
                <span style={{
                    width: 18,
                    height: 18,
                    borderRadius: 6,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                    fontSize: 8,
                    fontWeight: 700,
                    letterSpacing: '0.06em',
                    color: c,
                    background: `${c}18`,
                    border: `1px solid ${c}24`,
                }}>
                    {nodeMark}
                </span>
                <div style={{ flex: 1, overflow: 'hidden' }}>
                    <div style={{
                        fontSize: 11, fontWeight: 600, color: 'var(--text1)',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>{name}</div>
                    <div style={{
                        fontSize: 8,
                        color: 'var(--text3)',
                        marginTop: 1,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                    }}>
                        {desc}
                    </div>
                </div>
                {/* Status dot */}
                <span style={{
                    width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
                    background: sc.dot,
                    boxShadow: sc.pulse ? `0 0 6px ${sc.dot}` : 'none',
                    animation: sc.pulse ? 'agentPulse 1.5s infinite' : 'none',
                }} />
                {info.sec && (
                    <span style={{
                        fontSize: 7, padding: '1px 4px', borderRadius: 3, fontWeight: 700,
                        background: info.sec === 'L1' ? 'rgba(88,166,255,0.15)' : info.sec === 'L2' ? 'rgba(63,185,80,0.15)' : 'rgba(210,153,34,0.15)',
                        color: info.sec === 'L1' ? '#58a6ff' : info.sec === 'L2' ? '#3fb950' : '#d29922',
                    }}>{info.sec}</span>
                )}
            </div>

            {/* ── Body ── */}
            <div style={{ padding: '5px 0 6px' }}>
                {/* Model & Type tags */}
                <div style={{
                    padding: '0 10px', marginBottom: 3,
                    display: 'flex', gap: 3, alignItems: 'center',
                    position: 'relative',
                }} ref={dropdownRef}>
                    {/* Model selector tag */}
                    <span
                        className="model-selector-tag"
                        style={{
                            padding: '1px 5px', borderRadius: 3, fontSize: 8,
                            background: assignedModel ? 'rgba(79,143,255,0.12)' : 'var(--tag-bg)',
                            color: assignedModel ? '#4f8fff' : 'var(--tag-text)',
                            border: `1px solid ${assignedModel ? 'rgba(79,143,255,0.2)' : 'var(--tag-border)'}`,
                            cursor: 'pointer',
                            display: 'inline-flex', alignItems: 'center', gap: 2,
                            userSelect: 'none',
                            transition: 'all 0.15s ease',
                        }}
                        onClick={(e) => {
                            e.stopPropagation();
                            setModelOpen(!modelOpen);
                        }}
                        title={lang === 'zh' ? '点击切换模型' : 'Click to change model'}
                    >
                        {displayModel}
                        <span style={{ fontSize: 6, opacity: 0.7, marginLeft: 1 }}>▼</span>
                    </span>
                    <span style={{
                        padding: '1px 5px', borderRadius: 3, fontSize: 8,
                        background: `${c}0c`, color: c + 'bb',
                        border: `1px solid ${c}15`,
                    }}>{nodeType}</span>

                    {/* Model dropdown */}
                    {modelOpen && (
                        <div className="model-dropdown" style={{
                            position: 'absolute',
                            top: '100%',
                            left: 10,
                            marginTop: 4,
                            zIndex: 1000,
                            width: 190,
                            maxHeight: 240,
                            overflowY: 'auto',
                            background: 'var(--surface-strong)',
                            backdropFilter: 'blur(24px)',
                            WebkitBackdropFilter: 'blur(24px)',
                            border: '1px solid var(--glass-border)',
                            borderRadius: 8,
                            boxShadow: '0 12px 40px rgba(0,0,0,0.5)',
                            padding: '4px 0',
                        }}>
                            {AVAILABLE_MODELS.map((m) => (
                                <div
                                    key={m.id}
                                    className="model-dropdown-item"
                                    style={{
                                        padding: '5px 10px',
                                        fontSize: 9,
                                        color: m.id === displayModel ? 'var(--blue)' : 'var(--text1)',
                                        background: m.id === displayModel ? 'rgba(79,143,255,0.1)' : 'transparent',
                                        cursor: 'pointer',
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        alignItems: 'center',
                                        transition: 'all 0.1s ease',
                                    }}
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        handleModelChange(m.id);
                                    }}
                                >
                                    <span style={{ fontWeight: m.id === displayModel ? 600 : 400 }}>{m.label}</span>
                                    <span style={{
                                        fontSize: 7, color: 'var(--text3)',
                                        padding: '1px 4px', borderRadius: 3,
                                        background: 'rgba(255,255,255,0.04)',
                                    }}>{m.provider}</span>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* Input Ports */}
                {info.inputs.map((port) => (
                    <div key={port.id} style={{
                        display: 'flex', alignItems: 'center',
                        padding: '4px 12px 4px 6px',
                        position: 'relative',
                        minHeight: 22,
                    }}>
                        <Handle
                            type="target"
                            position={Position.Left}
                            id={port.id}
                            style={{
                                width: 9, height: 9,
                                borderRadius: '50%',
                                border: `2px solid ${c}`,
                                background: 'var(--node-bg)',
                                left: 10,
                                transition: 'all 0.15s ease',
                            }}
                        />
                        <span style={{
                            fontSize: 9, color: 'var(--text3)', marginLeft: 6,
                            display: 'flex', alignItems: 'center', gap: 3,
                        }}>
                            <span style={{ width: 3, height: 3, borderRadius: '50%', background: c, display: 'inline-block' }} />
                            {port.label}
                        </span>
                    </div>
                ))}

                {/* Output Ports */}
                {info.outputs.map((port) => (
                    <div key={port.id} style={{
                        display: 'flex', alignItems: 'center', justifyContent: 'flex-end',
                        padding: '4px 6px 4px 12px',
                        position: 'relative',
                        minHeight: 22,
                    }}>
                        <span style={{
                            fontSize: 9, color: 'var(--text3)', marginRight: 6,
                            display: 'flex', alignItems: 'center', gap: 3,
                        }}>
                            {port.label}
                            <span style={{ width: 3, height: 3, borderRadius: '50%', background: c, display: 'inline-block' }} />
                        </span>
                        <Handle
                            type="source"
                            position={Position.Right}
                            id={port.id}
                            style={{
                                width: 9, height: 9,
                                borderRadius: '50%',
                                border: `2px solid ${c}`,
                                background: 'var(--node-bg)',
                                right: 10,
                                transition: 'all 0.15s ease',
                            }}
                        />
                    </div>
                ))}

                {/* ── Progress Bar ── */}
                {progress > 0 && isRunning && (
                    <div style={{
                        margin: '4px 10px 0', height: 3, borderRadius: 2,
                        background: 'var(--node-divider)', overflow: 'hidden',
                    }}>
                        <div style={{
                            height: '100%', borderRadius: 2, width: `${Math.min(progress, 100)}%`,
                            background: `linear-gradient(90deg, ${sc.color}, #a855f7)`,
                            transition: 'width 0.3s ease',
                        }} />
                    </div>
                )}

                {/* ── Execution Metrics Footer ── */}
                {(hasMetrics || rawStatus !== 'idle') && rawStatus !== 'idle' && (
                    <div style={{
                        margin: '4px 10px 2px',
                        padding: '4px 6px',
                        borderRadius: 5,
                        background: `${sc.color}08`,
                        border: `1px solid ${sc.color}12`,
                        display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center',
                    }}>
                        {/* Duration */}
                        {startedAt > 0 && (
                            <span style={{
                                fontSize: 7, color: 'var(--text3)',
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                            }}>
                                {lang === 'zh' ? '耗时' : 'Time'} {formatDuration(startedAt, endedAt)}
                            </span>
                        )}

                        {/* Tokens */}
                        {tokensUsed > 0 && (
                            <span style={{
                                fontSize: 7, color: 'var(--text3)',
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                            }}>
                                Tokens {formatTokens(tokensUsed)}
                            </span>
                        )}

                        {/* Cost */}
                        {cost > 0 && (
                            <span style={{
                                fontSize: 7, color: 'var(--text3)',
                                display: 'inline-flex', alignItems: 'center', gap: 2,
                            }}>
                                {lang === 'zh' ? '费用' : 'Cost'} {formatCost(cost)}
                            </span>
                        )}

                        {/* Spacer */}
                        <span style={{ flex: 1 }} />

                        {/* Status label */}
                        <span style={{
                            fontSize: 7, fontWeight: 600,
                            color: sc.color,
                            display: 'inline-flex', alignItems: 'center',
                            padding: '1px 5px',
                            borderRadius: 999,
                            background: `${sc.color}12`,
                            border: `1px solid ${sc.color}18`,
                        }}>
                            {lang === 'zh' ? sc.label_zh : sc.label_en}
                        </span>
                    </div>
                )}

                {/* ── Output Preview — click to expand ── */}
                {displayOutput && (
                    <div
                        style={{
                            margin: '3px 10px 0', fontSize: 8, color: 'var(--text3)', lineHeight: 1.3,
                            cursor: 'pointer',
                            ...(outputExpanded ? {
                                maxHeight: 120, overflowY: 'auto' as const, whiteSpace: 'pre-wrap' as const,
                                wordBreak: 'break-all' as const,
                                background: 'rgba(0,0,0,0.2)', borderRadius: 4, padding: '4px 6px',
                                fontFamily: 'monospace',
                            } : {
                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' as const,
                            }),
                        }}
                        onClick={(e) => { e.stopPropagation(); setOutputExpanded(!outputExpanded); }}
                        title={lang === 'zh' ? '点击展开/收起' : 'Click to expand/collapse'}
                    >
                        {outputExpanded ? displayOutput.substring(0, 1000) : `${displayOutput.substring(0, 60)}${displayOutput.length > 60 ? '...' : ''}`}
                    </div>
                )}
            </div>

            <style>{`
                @keyframes agentPulse { 0%,100% { opacity:1 } 50% { opacity:0.3 } }
            `}</style>
        </div>
    );
}

export default memo(AgentNode);
