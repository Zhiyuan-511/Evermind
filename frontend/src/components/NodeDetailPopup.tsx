'use client';

import React, { useEffect, useState } from 'react';
import type { CanvasNodeStatus } from '@/lib/types';
import { NODE_TYPES } from '@/lib/types';
import {
    buildReadableCurrentWork,
    describeNodeActivity,
    formatPhaseLabel,
    formatSkillLabel,
    getStructuredOutputSections,
} from '@/lib/nodeOutputHumanizer';

interface NodeDetailPopupProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
    nodeData: {
        id: string;
        nodeType?: string;
        label?: string;
        status?: string;
        progress?: number;
        phase?: string;
        model?: string;
        assignedModel?: string;
        lastOutput?: string;
        outputSummary?: string;
        taskDescription?: string;
        loadedSkills?: string[];
        tokensUsed?: number;
        promptTokens?: number;
        completionTokens?: number;
        cost?: number;
        startedAt?: number;
        endedAt?: number;
        durationSeconds?: number;
        subtaskId?: string;
        log?: Array<{ ts: number; msg: string; type: string }>;
    } | null;
}

type TimelineEntry = {
    ts: number;
    text: string;
    type: 'info' | 'ok' | 'error' | 'sys';
    title?: string;
};

const STATUS_COLORS: Record<string, { color: string; labelZh: string; labelEn: string }> = {
    idle: { color: '#555', labelZh: '空闲', labelEn: 'Idle' },
    queued: { color: '#8b8fa3', labelZh: '排队中', labelEn: 'Queued' },
    running: { color: '#4f8fff', labelZh: '执行中', labelEn: 'Running' },
    passed: { color: '#40d67c', labelZh: '已完成', labelEn: 'Passed' },
    done: { color: '#40d67c', labelZh: '已完成', labelEn: 'Done' },
    failed: { color: '#ff4f6a', labelZh: '失败', labelEn: 'Failed' },
    error: { color: '#ff4f6a', labelZh: '错误', labelEn: 'Error' },
    blocked: { color: '#ff9b47', labelZh: '阻塞', labelEn: 'Blocked' },
    waiting_approval: { color: '#f59e0b', labelZh: '待审核', labelEn: 'Awaiting Review' },
    skipped: { color: '#666', labelZh: '已跳过', labelEn: 'Skipped' },
};

function formatDuration(startMs: number, endMs?: number): string {
    if (!startMs) return '—';
    const end = endMs || Date.now();
    const sec = Math.max(0, Math.round((end - startMs) / 1000));
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
    return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function formatTokens(n: number | undefined): string {
    if (!n || n <= 0) return '—';
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return String(n);
}

function formatCost(n: number | undefined): string {
    if (!n || n <= 0) return '—';
    if (n < 0.01) return `$${n.toFixed(4)}`;
    return `$${n.toFixed(2)}`;
}

function formatClock(ts: number, lang: 'en' | 'zh'): string {
    if (!ts) return '--:--:--';
    return new Date(ts).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-US', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
    });
}

function nodeTypeLabel(nodeType: string, lang: 'en' | 'zh'): string {
    const labels: Record<string, { zh: string; en: string }> = {
        builder: { zh: '构建者', en: 'Builder' },
        reviewer: { zh: '审查员', en: 'Reviewer' },
        tester: { zh: '测试员', en: 'Tester' },
        deployer: { zh: '部署员', en: 'Deployer' },
        planner: { zh: '规划师', en: 'Planner' },
        analyst: { zh: '分析师', en: 'Analyst' },
        debugger: { zh: '调试员', en: 'Debugger' },
    };
    return labels[nodeType] ? (lang === 'zh' ? labels[nodeType].zh : labels[nodeType].en) : nodeType;
}

function entryToneColor(type: string): { border: string; bg: string; text: string } {
    if (type === 'error') {
        return { border: 'rgba(255,79,106,0.35)', bg: 'rgba(255,79,106,0.08)', text: '#ff97a9' };
    }
    if (type === 'ok') {
        return { border: 'rgba(64,214,124,0.35)', bg: 'rgba(64,214,124,0.08)', text: '#9ce9bc' };
    }
    if (type === 'sys') {
        return { border: 'rgba(168,85,247,0.35)', bg: 'rgba(168,85,247,0.08)', text: '#d8b4fe' };
    }
    return { border: 'rgba(255,255,255,0.08)', bg: 'rgba(255,255,255,0.03)', text: 'var(--text2)' };
}

function normalizeEntries(
    logs: Array<{ ts: number; msg: string; type: string }>,
    taskDesc: string,
    loadedSkills: string[],
    outputSummary: string,
    lastOutput: string,
    nodeType: string,
    status: string,
    startedAt: number,
    endedAt: number,
    lang: 'en' | 'zh',
): TimelineEntry[] {
    const entries: TimelineEntry[] = [];
    const seen = new Set<string>();

    const pushEntry = (entry: TimelineEntry) => {
        const text = String(entry.text || '').trim();
        if (!text) return;
        const key = `${entry.title || ''}::${text}`;
        if (seen.has(key)) return;
        seen.add(key);
        entries.push({
            ts: entry.ts || Date.now(),
            text,
            type: entry.type,
            ...(entry.title ? { title: entry.title } : {}),
        });
    };

    if (taskDesc) {
        pushEntry({
            ts: startedAt || Date.now(),
            type: 'sys',
            title: lang === 'zh' ? '任务说明' : 'Task Brief',
            text: taskDesc,
        });
    }

    if (loadedSkills.length > 0) {
        pushEntry({
            ts: startedAt || Date.now(),
            type: 'sys',
            title: lang === 'zh' ? '技能加载' : 'Skills Loaded',
            text: loadedSkills.map(formatSkillLabel).join(', '),
        });
    }

    [...logs]
        .filter((log) => String(log.msg || '').trim())
        .sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0))
        .forEach((log) => {
            const descriptor = describeNodeActivity(String(log.msg || '').trim(), lang, { nodeType, status });
            if (!descriptor || descriptor.lowSignal) return;
            pushEntry({
                ts: Number(log.ts || Date.now()),
                type: descriptor.type || (String(log.type || 'info') as TimelineEntry['type']),
                title: descriptor.title,
                text: descriptor.text,
            });
        });

    const pushOutputEntries = (rawText: string, titleZh: string, titleEn: string, tone: TimelineEntry['type']) => {
        const normalized = String(rawText || '').trim();
        if (!normalized) return;

        const structuredSections = getStructuredOutputSections(normalized, { lang, nodeType, status });
        if (structuredSections.length > 0) {
            structuredSections.forEach((section) => {
                pushEntry({
                    ts: endedAt || startedAt || Date.now(),
                    type: section.type,
                    title: section.title,
                    text: section.text,
                });
            });
            return;
        }

        const descriptor = describeNodeActivity(normalized, lang, { nodeType, status });
        const humanText = descriptor?.text || normalized;
        pushEntry({
            ts: endedAt || startedAt || Date.now(),
            type: descriptor?.type || tone,
            title: descriptor?.title || (lang === 'zh' ? titleZh : titleEn),
            text: humanText.slice(0, 2400),
        });
    };

    pushOutputEntries(
        outputSummary,
        '结果摘要',
        'Outcome Summary',
        status === 'failed' || status === 'error' ? 'error' : 'ok',
    );

    const normalizedLastOutput = String(lastOutput || '').trim();
    const normalizedSummary = String(outputSummary || '').trim();
    if (normalizedLastOutput && normalizedLastOutput !== normalizedSummary) {
        pushOutputEntries(
            normalizedLastOutput,
            '关键产出',
            'Key Output',
            status === 'failed' || status === 'error' ? 'error' : 'info',
        );
    }

    return entries;
}

export default function NodeDetailPopup({ open, onClose, lang, nodeData }: NodeDetailPopupProps) {
    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);
    const [, setTick] = useState(0);

    useEffect(() => {
        if (!open || !nodeData || nodeData.status !== 'running') return;
        const timer = setInterval(() => setTick((v) => v + 1), 1000);
        return () => clearInterval(timer);
    }, [open, nodeData]);

    if (!open || !nodeData) return null;

    const nodeType = nodeData.nodeType || 'builder';
    const info = NODE_TYPES[nodeType];
    const status = (nodeData.status || 'idle') as CanvasNodeStatus;
    const sc = STATUS_COLORS[status] || STATUS_COLORS.idle;
    const accent = info?.color || '#666';
    const label = nodeData.label || (lang === 'zh' ? info?.label_zh : info?.label_en) || nodeType;
    const progress = Math.max(0, Math.min(100, Number(nodeData.progress || (status === 'running' ? 5 : 0))));
    const model = nodeData.assignedModel || nodeData.model || 'gpt-5.4';
    const phase = String(nodeData.phase || '').trim();
    const phaseLabel = formatPhaseLabel(phase, lang);
    const taskDesc = String(nodeData.taskDescription || '').trim();
    const outputSummary = String(nodeData.outputSummary || '').trim();
    const lastOutput = String(nodeData.lastOutput || '').trim();
    const loadedSkills = Array.isArray(nodeData.loadedSkills) ? nodeData.loadedSkills.filter(Boolean) : [];
    const tokensUsed = Number(nodeData.tokensUsed || 0);
    const promptTokens = Number(nodeData.promptTokens || 0);
    const completionTokens = Number(nodeData.completionTokens || 0);
    const cost = Number(nodeData.cost || 0);
    const startedAt = Number(nodeData.startedAt || 0);
    const endedAt = Number(nodeData.endedAt || 0);
    const logs = Array.isArray(nodeData.log) ? nodeData.log : [];
    const durationText = (() => {
        if (nodeData.durationSeconds && nodeData.durationSeconds > 0) {
            const seconds = Number(nodeData.durationSeconds);
            if (seconds < 60) return `${seconds}s`;
            if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
            return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
        }
        if (startedAt) return formatDuration(startedAt, endedAt || undefined);
        return '—';
    })();

    const currentWork = buildReadableCurrentWork({
        lang,
        nodeType,
        status,
        phase,
        taskDescription: taskDesc,
        outputSummary,
        lastOutput,
        loadedSkills,
        logs,
        durationText,
    });

    const timelineEntries = normalizeEntries(
        logs,
        taskDesc,
        loadedSkills,
        outputSummary,
        lastOutput,
        nodeType,
        status,
        startedAt,
        endedAt,
        lang,
    );

    return (
        <div
            onClick={onClose}
            style={{
                position: 'fixed',
                inset: 0,
                zIndex: 9999,
                background: 'rgba(0,0,0,0.55)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                animation: 'fadeIn 0.2s ease',
            }}
        >
            <div
                onClick={(e) => e.stopPropagation()}
                style={{
                    width: 540,
                    maxWidth: 'calc(100vw - 24px)',
                    maxHeight: '84vh',
                    overflow: 'hidden',
                    background: 'var(--surface-strong, #1a1a2e)',
                    backdropFilter: 'blur(24px)',
                    WebkitBackdropFilter: 'blur(24px)',
                    border: `1.5px solid ${accent}33`,
                    borderRadius: 18,
                    boxShadow: `0 18px 72px rgba(0,0,0,0.5), 0 0 24px ${accent}12`,
                    display: 'flex',
                    flexDirection: 'column',
                    animation: 'slideUp 0.25s ease',
                }}
            >
                <div
                    style={{
                        padding: '16px 18px 12px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 10,
                        borderBottom: `1px solid ${accent}20`,
                        background: `linear-gradient(135deg, ${accent}14, transparent)`,
                    }}
                >
                    <span
                        style={{
                            width: 34,
                            height: 34,
                            borderRadius: 10,
                            display: 'inline-flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            fontSize: 12,
                            fontWeight: 800,
                            color: accent,
                            background: `${accent}18`,
                            border: `1.5px solid ${accent}2d`,
                            flexShrink: 0,
                        }}
                    >
                        {info?.icon || nodeType.slice(0, 2).toUpperCase()}
                    </span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {label}
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--text3)', marginTop: 2 }}>
                            {nodeTypeLabel(nodeType, lang)} · {model}
                        </div>
                    </div>
                    <span
                        style={{
                            padding: '4px 10px',
                            borderRadius: 999,
                            fontSize: 11,
                            fontWeight: 700,
                            color: sc.color,
                            background: `${sc.color}18`,
                            border: `1px solid ${sc.color}25`,
                            animation: status === 'running' ? 'pulse 1.5s infinite' : 'none',
                        }}
                    >
                        {lang === 'zh' ? sc.labelZh : sc.labelEn}
                    </span>
                    <button
                        onClick={onClose}
                        style={{
                            width: 30,
                            height: 30,
                            borderRadius: 9,
                            border: '1px solid rgba(255,255,255,0.08)',
                            background: 'rgba(255,255,255,0.04)',
                            color: 'var(--text3)',
                            cursor: 'pointer',
                            fontSize: 14,
                        }}
                    >
                        ×
                    </button>
                </div>

                <div style={{ padding: '10px 18px 0' }}>
                    <div style={{ height: 6, borderRadius: 999, background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
                        <div
                            style={{
                                height: '100%',
                                width: `${progress}%`,
                                borderRadius: 999,
                                background: status === 'running'
                                    ? `linear-gradient(90deg, ${accent}, #4f8fff)`
                                    : status === 'passed' || status === 'done' || status === 'skipped'
                                        ? '#40d67c'
                                        : sc.color,
                                transition: 'width 0.4s ease',
                            }}
                        />
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 5, fontSize: 10, color: 'var(--text3)' }}>
                        <span>{tr('进度', 'Progress')}</span>
                        <span style={{ color: sc.color, fontWeight: 700 }}>{Math.round(progress)}%</span>
                    </div>
                </div>

                <div style={{ padding: '12px 18px', display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
                    <div style={{ padding: '9px 10px', borderRadius: 10, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.05)' }}>
                        <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 4 }}>{tr('运行时间', 'Time')}</div>
                        <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text1)' }}>{durationText}</div>
                    </div>
                    <div style={{ padding: '9px 10px', borderRadius: 10, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.05)' }}>
                        <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 4 }}>{tr('Token 消耗', 'Tokens')}</div>
                        <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text1)' }}>{formatTokens(tokensUsed)}</div>
                        {(promptTokens > 0 || completionTokens > 0) && (
                            <div style={{ fontSize: 9, color: 'var(--text3)', marginTop: 3 }}>
                                {`P ${formatTokens(promptTokens)} · C ${formatTokens(completionTokens)}`}
                            </div>
                        )}
                    </div>
                    <div style={{ padding: '9px 10px', borderRadius: 10, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.05)' }}>
                        <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 4 }}>{tr('费用', 'Cost')}</div>
                        <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text1)' }}>{formatCost(cost)}</div>
                    </div>
                </div>

                <div style={{ flex: 1, overflow: 'auto', padding: '0 18px 18px' }}>
                    <div
                        style={{
                            borderRadius: 14,
                            padding: '14px 14px 12px',
                            background: 'rgba(79,143,255,0.06)',
                            border: '1px solid rgba(79,143,255,0.16)',
                            marginBottom: 12,
                        }}
                    >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 8 }}>
                            <div style={{ fontSize: 12, fontWeight: 700, color: '#93c5fd' }}>
                                {tr('目前正在做的事情', 'Current Work')}
                            </div>
                            {phaseLabel && (
                                <span
                                    style={{
                                        padding: '3px 8px',
                                        borderRadius: 999,
                                        fontSize: 10,
                                        color: '#bfdbfe',
                                        background: 'rgba(79,143,255,0.1)',
                                        border: '1px solid rgba(79,143,255,0.16)',
                                        whiteSpace: 'nowrap',
                                    }}
                                >
                                    {phaseLabel}
                                </span>
                            )}
                        </div>
                        {taskDesc && (
                            <div style={{ fontSize: 11, color: 'var(--text2)', lineHeight: 1.6, marginBottom: 8 }}>
                                <span style={{ color: 'var(--text3)' }}>{tr('任务', 'Task')}:</span> {taskDesc}
                            </div>
                        )}
                        {loadedSkills.length > 0 && (
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
                                {loadedSkills.map((skill) => (
                                    <span
                                        key={skill}
                                        style={{
                                            padding: '3px 8px',
                                            borderRadius: 999,
                                            fontSize: 10,
                                            color: '#e9d5ff',
                                            background: 'rgba(168,85,247,0.12)',
                                            border: '1px solid rgba(168,85,247,0.2)',
                                        }}
                                    >
                                        {formatSkillLabel(skill)}
                                    </span>
                                ))}
                            </div>
                        )}
                        <div style={{ fontSize: 12, color: 'var(--text2)', lineHeight: 1.75, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                            {currentWork}
                        </div>
                    </div>

                    <div
                        style={{
                            borderRadius: 14,
                            padding: '14px',
                            background: 'rgba(255,255,255,0.03)',
                            border: '1px solid rgba(255,255,255,0.06)',
                        }}
                    >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 10 }}>
                            <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text1)' }}>
                                {tr('已完成的工作明细', 'Completed Work Details')}
                            </div>
                            <div style={{ fontSize: 10, color: 'var(--text3)' }}>
                                {timelineEntries.length} {tr('条记录', 'entries')}
                            </div>
                        </div>
                        <div style={{ maxHeight: 360, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 8, paddingRight: 2 }}>
                            {timelineEntries.length === 0 ? (
                                <div style={{ fontSize: 11, color: 'var(--text3)', lineHeight: 1.7 }}>
                                    {tr('该节点暂时还没有沉淀出可展示的执行明细。随着任务推进，这里会累积技能加载、浏览器访问、文件产出、质量校验和最终结果。', 'No detailed execution entries are available yet. As the node runs, this area will accumulate skills, browser activity, file output, quality checks, and the final result.')}
                                </div>
                            ) : (
                                timelineEntries.map((entry, index) => {
                                    const tone = entryToneColor(entry.type);
                                    return (
                                        <div
                                            key={`${entry.ts}-${index}`}
                                            style={{
                                                borderRadius: 12,
                                                padding: '10px 11px',
                                                background: tone.bg,
                                                border: `1px solid ${tone.border}`,
                                            }}
                                        >
                                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, marginBottom: 5 }}>
                                                <div style={{ fontSize: 10, fontWeight: 700, color: tone.text }}>
                                                    {entry.title || tr('执行记录', 'Execution Record')}
                                                </div>
                                                <div style={{ fontSize: 10, color: 'var(--text3)', whiteSpace: 'nowrap' }}>
                                                    {formatClock(entry.ts, lang)}
                                                </div>
                                            </div>
                                            <div style={{ fontSize: 11, color: 'var(--text2)', lineHeight: 1.7, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                                {entry.text}
                                            </div>
                                        </div>
                                    );
                                })
                            )}
                        </div>
                    </div>
                </div>
            </div>

            <style>{`
                @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
                @keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
                @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.55; } }
            `}</style>
        </div>
    );
}
