'use client';

import { useEffect, useState, type CSSProperties } from 'react';
import type { ChatMessage } from '@/lib/types';
import { normalizeRuntimeModeForDisplay, runtimeLabelForDisplay } from '@/lib/runtimeDisplay';

interface TaskSummaryStripProps {
    running: boolean;
    lang: 'en' | 'zh';
    messages: ChatMessage[];
    runtimeMode?: string;
    taskTitle?: string | null;
    taskStatus?: string | null;
    activeNodeLabels?: string[];
    completedNodes?: number;
    runningNodes?: number;
    totalNodes?: number;
    startedAt?: number | null;
}

const STATUS_DOTS: Record<string, { color: string; label_en: string; label_zh: string }> = {
    executing:        { color: '#3b82f6', label_en: 'Executing', label_zh: '执行中' },
    running:          { color: '#3b82f6', label_en: 'Running', label_zh: '运行中' },
    review:           { color: '#f59e0b', label_en: 'Review', label_zh: '审核中' },
    selfcheck:        { color: '#06b6d4', label_en: 'Self-check', label_zh: '自检中' },
    done:             { color: '#22c55e', label_en: 'Done', label_zh: '已完成' },
    failed:           { color: '#ef4444', label_en: 'Failed', label_zh: '失败' },
    waiting_review:   { color: '#f59e0b', label_en: 'Awaiting Review', label_zh: '等待审核' },
    waiting_selfcheck:{ color: '#06b6d4', label_en: 'Awaiting Check', label_zh: '等待自检' },
};

function formatElapsed(startMs: number): string {
    if (!startMs) return '';
    const sec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}m${s > 0 ? ` ${s}s` : ''}`;
}

export default function TaskSummaryStrip({
    running,
    lang,
    messages,
    runtimeMode,
    taskTitle,
    taskStatus,
    activeNodeLabels = [],
    completedNodes = 0,
    runningNodes = 0,
    totalNodes = 0,
    startedAt,
}: TaskSummaryStripProps) {
    const tr = (zh: string, en: string) => lang === 'zh' ? zh : en;

    let latestGoal = '';
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        if (messages[i]?.role === 'user' && messages[i]?.content?.trim()) {
            latestGoal = messages[i].content.trim();
            break;
        }
    }

    let fallbackNodeLabel = '';
    for (let i = messages.length - 1; i >= 0; i -= 1) {
        const msg = messages[i];
        if (msg?.sender === 'console' || msg?.role === 'user') continue;
        const sender = String(msg?.sender || '').trim();
        if (sender && sender !== 'System') {
            fallbackNodeLabel = sender;
            break;
        }
    }

    // V4.3 PERF: Reduced from 1s to 5s to cut React re-renders
    const [, setTick] = useState(0);
    const shouldRender = running || !!taskTitle;
    useEffect(() => {
        if (!shouldRender) return;
        const timer = setInterval(() => setTick(t => t + 1), 5000);
        return () => clearInterval(timer);
    }, [shouldRender]);

    // Don't render if nothing is running and no task
    if (!shouldRender) return null;

    const effectiveStatus = taskStatus || (running ? 'running' : 'executing');
    const runStatus = STATUS_DOTS[effectiveStatus] || STATUS_DOTS.executing;
    const startTime = startedAt || 0;
    const normalizedStart = startTime < 10_000_000_000 ? startTime * 1000 : startTime;
    const normalizedRuntimeMode = normalizeRuntimeModeForDisplay(runtimeMode);
    const runtimeLabel = runtimeLabelForDisplay(runtimeMode);

    const containerStyle: CSSProperties = {
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '6px 12px',
        background: 'rgba(91, 140, 255, 0.04)',
        borderBottom: '1px solid var(--glass-border)',
        minHeight: 36,
        fontSize: 11,
        transition: 'all 0.2s',
    };

    const dotStyle: CSSProperties = {
        width: 7,
        height: 7,
        borderRadius: '50%',
        background: runStatus.color,
        flexShrink: 0,
        animation: effectiveStatus === 'running' ? 'pulse 1.5s ease-in-out infinite' : 'none',
    };

    const chipStyle: CSSProperties = {
        padding: '2px 6px',
        borderRadius: 4,
        fontSize: 9,
        fontWeight: 600,
        background: `${runStatus.color}14`,
        color: runStatus.color,
        border: `1px solid ${runStatus.color}28`,
        whiteSpace: 'nowrap',
    };

    return (
        <div style={containerStyle}>
            {/* Status dot */}
            <span style={dotStyle} />

            {/* Task title */}
            <span style={{
                fontWeight: 600,
                color: 'var(--text1)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                maxWidth: 140,
            }}>
                {taskTitle || tr('任务执行中', 'Task running')}
                {!taskTitle && latestGoal ? ` · ${latestGoal}` : ''}
            </span>

            {/* Status chip */}
            <span style={chipStyle}>
                {lang === 'zh' ? runStatus.label_zh : runStatus.label_en}
            </span>

            {/* Current node(s) */}
            {(activeNodeLabels.length > 0 || fallbackNodeLabel) && (
                <span style={{
                    color: 'var(--text2)',
                    fontSize: 10,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                }}>
                    {activeNodeLabels.length > 0
                        ? activeNodeLabels.join(', ')
                        : fallbackNodeLabel}
                </span>
            )}

            {/* Runtime badge */}
            {normalizedRuntimeMode && runtimeLabel && (
                <span style={{
                    fontSize: 8,
                    fontWeight: 700,
                    padding: '1px 5px',
                    borderRadius: 3,
                    background: normalizedRuntimeMode === 'openclaw' ? 'rgba(168, 85, 247, 0.12)' : 'rgba(91, 140, 255, 0.08)',
                    color: normalizedRuntimeMode === 'openclaw' ? '#a855f7' : 'var(--text3)',
                    border: `1px solid ${normalizedRuntimeMode === 'openclaw' ? 'rgba(168, 85, 247, 0.2)' : 'rgba(91, 140, 255, 0.12)'}`,
                    textTransform: 'uppercase',
                    letterSpacing: '0.05em',
                }}>
                    {runtimeLabel}
                </span>
            )}

            {/* Progress */}
            {(totalNodes > 0 || normalizedStart > 0) && (
                <span style={{
                    color: 'var(--text3)',
                    fontSize: 9,
                    marginLeft: 'auto',
                    whiteSpace: 'nowrap',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                }}>
                    {totalNodes > 0 && (() => {
                        const queued = Math.max(0, totalNodes - completedNodes - runningNodes);
                        if (completedNodes === 0 && runningNodes === 0) {
                            return <span>0/{totalNodes} {tr('节点', 'nodes')}</span>;
                        }
                        return (
                            <span>
                                {completedNodes > 0 && <>{completedNodes} {tr('完成', 'done')}</>}
                                {completedNodes > 0 && runningNodes > 0 && ' / '}
                                {runningNodes > 0 && <span style={{ color: '#3b82f6' }}>{runningNodes} {tr('执行中', 'active')}</span>}
                                {queued > 0 && <> / {queued} {tr('排队', 'queued')}</>}
                            </span>
                        );
                    })()}
                    {normalizedStart > 0 && (
                        <span style={{ color: 'var(--text3)' }}>
                            {formatElapsed(normalizedStart)}
                        </span>
                    )}
                </span>
            )}
        </div>
    );
}
