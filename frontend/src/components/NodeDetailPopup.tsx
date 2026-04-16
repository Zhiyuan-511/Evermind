'use client';

import React, { useEffect, useMemo, useState } from 'react';
import type { ArtifactRecord, CanvasNodeStatus, NodeExecutionRecord } from '@/lib/types';
import { NODE_TYPES } from '@/lib/types';
import { getNodeExecution, listArtifacts } from '@/lib/api';
import {
    buildReadableCurrentWork,
    describeNodeActivity,
    formatPhaseLabel,
    formatSkillLabel,
    getStructuredOutputSections,
} from '@/lib/nodeOutputHumanizer';

type NodeDetailInput = {
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
    referenceUrls?: string[];
    currentAction?: string;
    workSummary?: string[];
    toolCallStats?: Record<string, number>;
    blockingReason?: string;
    latestReviewDecision?: string;
    artifactIds?: string[];
    reportArtifactIds?: string[];
    handoffArtifactIds?: string[];
    dossierArtifactId?: string;
    summaryArtifactId?: string;
    tokensUsed?: number;
    promptTokens?: number;
    completionTokens?: number;
    cost?: number;
    startedAt?: number;
    endedAt?: number;
    durationSeconds?: number;
    subtaskId?: string;
    nodeExecutionId?: string;
    log?: Array<{ ts: number; msg: string; type: string }>;
    codeLines?: number;
    totalLines?: number;
    codeKb?: number;
    codeLanguages?: string[];
    modelLatencyMs?: number;
} | null;

interface NodeDetailPopupProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
    nodeData: NodeDetailInput;
}

type TimelineEntry = {
    ts: number;
    text: string;
    type: 'info' | 'ok' | 'error' | 'sys';
    title?: string;
};

type DetailTab = 'overview' | 'timeline' | 'refs' | 'reports';

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
    cancelled: { color: '#78716c', labelZh: '已取消', labelEn: 'Cancelled' },
};

const ARTIFACT_TYPE_META: Record<string, { labelZh: string; labelEn: string; tone: string }> = {
    execution_dossier: { labelZh: '执行档案', labelEn: 'Execution Dossier', tone: '#06b6d4' },
    node_summary_report: { labelZh: '节点总结', labelEn: 'Node Summary', tone: '#38bdf8' },
    source_bundle: { labelZh: '源码参考包', labelEn: 'Source Bundle', tone: '#8b5cf6' },
    builder_handoff_report: { labelZh: 'Builder 交接报告', labelEn: 'Builder Handoff', tone: '#22c55e' },
    merge_manifest: { labelZh: '合并清单', labelEn: 'Merge Manifest', tone: '#f59e0b' },
    merge_execution_report: { labelZh: '合并执行报告', labelEn: 'Merge Report', tone: '#fb7185' },
    review_rollback_report: { labelZh: '审查打回报告', labelEn: 'Rollback Report', tone: '#ef4444' },
    deployment_receipt: { labelZh: '部署回执', labelEn: 'Deployment Receipt', tone: '#ec4899' },
    review_result: { labelZh: '审查结果', labelEn: 'Review Result', tone: '#06b6d4' },
    browser_capture: { labelZh: '浏览器证据', labelEn: 'Browser Capture', tone: '#f97316' },
    browser_trace: { labelZh: '浏览器 Trace', labelEn: 'Browser Trace', tone: '#f97316' },
    qa_session_capture: { labelZh: 'QA 证据', labelEn: 'QA Capture', tone: '#eab308' },
    qa_session_video: { labelZh: 'QA 录屏', labelEn: 'QA Video', tone: '#eab308' },
    qa_session_log: { labelZh: 'QA 日志', labelEn: 'QA Log', tone: '#eab308' },
    report: { labelZh: '报告', labelEn: 'Report', tone: '#94a3b8' },
};

function formatDuration(startTs: number, endTs?: number): string {
    if (!startTs) return '—';
    // v3.5.2: auto-detect seconds vs milliseconds
    const startMs = startTs < 10_000_000_000 ? startTs * 1000 : startTs;
    const endRaw = endTs || Date.now();
    const endMs = endRaw < 10_000_000_000 ? endRaw * 1000 : endRaw;
    const sec = Math.max(0, Math.round((endMs - startMs) / 1000));
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
    return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function formatTokens(n: number | undefined): string {
    if (n === undefined || n === null || isNaN(n)) return '—';
    if (n === 0) return '0';
    if (n < 0) return '—';
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return String(n);
}

function formatCost(n: number | undefined): string {
    if (n === undefined || n === null || isNaN(n)) return '—';
    if (n === 0) return '$0.00';
    if (n < 0) return '—';
    if (n < 0.01) return `$${n.toFixed(4)}`;
    return `$${n.toFixed(2)}`;
}

function formatClock(ts: number, lang: 'en' | 'zh'): string {
    if (!ts) return '--:--:--';
    // v3.5.2: auto-detect seconds vs milliseconds (same logic as formatArtifactTs)
    const ms = ts < 10_000_000_000 ? ts * 1000 : ts;
    return new Date(ms).toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-US', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
    });
}

function formatArtifactTs(epochSec: number, lang: 'en' | 'zh'): string {
    if (!epochSec) return '—';
    const ts = epochSec < 10_000_000_000 ? epochSec * 1000 : epochSec;
    return new Date(ts).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
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
        uidesign: { zh: '设计节点', en: 'UI Design' },
        scribe: { zh: '文档节点', en: 'Scribe' },
        merger: { zh: '合并器', en: 'Merger' },
        polisher: { zh: '抛光器', en: 'Polisher' },
        imagegen: { zh: '图像生成', en: 'Image Gen' },
        spritesheet: { zh: '精灵图生成', en: 'Spritesheet' },
        assetimport: { zh: '素材导入', en: 'Asset Import' },
    };
    return labels[nodeType] ? (lang === 'zh' ? labels[nodeType].zh : labels[nodeType].en) : nodeType;
}

// v3.0: Tool call icon mapping for timeline
function toolIcon(toolName: string): string {
    const icons: Record<string, string> = {
        file_read: '📁', file_write: '✏️', file_edit: '✏️',
        grep_search: '🔍', web_search: '🌐', web_fetch: '📥',
        bash: '💻', browser: '🖥️', source_crawler: '🕷️',
        context_compress: '🗜️', sub_agent: '🤖',
        write_file: '✏️', read_file: '📁',
    };
    return icons[toolName] || '⚙️';
}

// v3.0: Detect tool mentions in timeline text
function extractToolMention(text: string): string | null {
    const toolPatterns = ['file_read', 'file_write', 'file_edit', 'read_file', 'write_file',
        'grep_search', 'web_search', 'web_fetch', 'source_fetch',
        'bash', 'shell', 'browser', 'file_ops', 'file_list', 'list_dir', 'source_crawler'];
    const lower = text.toLowerCase();
    for (const tool of toolPatterns) {
        if (lower.includes(tool)) return tool;
    }
    if (lower.includes('file_ops')) return 'file_write';
    return null;
}

// v3.0: Simple Markdown renderer (no external deps)
function SimpleMarkdown({ content, accentColor }: { content: string; accentColor?: string }) {
    const accent = accentColor || '#4f8fff';
    const lines = content.split('\n');
    const elements: React.ReactNode[] = [];
    let inCodeBlock = false;
    let codeLines: string[] = [];
    let codeLang = '';
    let listItems: string[] = [];
    let blockKey = 0;

    const flushList = () => {
        if (listItems.length === 0) return;
        elements.push(
            <ul key={`list-${blockKey++}`} style={{ margin: '6px 0', paddingLeft: 20, color: 'var(--text1)' }}>
                {listItems.map((item, i) => (
                    <li key={i} style={{ fontSize: 13, lineHeight: 1.7, marginBottom: 2 }}>
                        {renderInline(item)}
                    </li>
                ))}
            </ul>
        );
        listItems = [];
    };

    const flushCode = () => {
        elements.push(
            <div key={`code-${blockKey++}`} style={{
                margin: '8px 0',
                borderRadius: 10,
                border: '1px solid rgba(255,255,255,0.08)',
                background: 'rgba(0,0,0,0.3)',
                overflow: 'hidden',
            }}>
                {codeLang ? (
                    <div style={{
                        padding: '4px 10px',
                        fontSize: 10,
                        fontWeight: 700,
                        color: accent,
                        background: 'rgba(255,255,255,0.04)',
                        borderBottom: '1px solid rgba(255,255,255,0.06)',
                        textTransform: 'uppercase',
                        letterSpacing: 0.5,
                    }}>{codeLang}</div>
                ) : null}
                <pre style={{
                    margin: 0,
                    padding: '10px 12px',
                    fontSize: 12,
                    lineHeight: 1.55,
                    color: '#e2e8f0',
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                    overflowX: 'auto',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                }}>{codeLines.join('\n')}</pre>
            </div>
        );
        codeLines = [];
        codeLang = '';
    };

    const renderInline = (text: string): React.ReactNode => {
        // Bold **text**
        const parts: React.ReactNode[] = [];
        const boldRegex = /\*\*(.+?)\*\*/g;
        let lastIndex = 0;
        let match;
        let partKey = 0;
        while ((match = boldRegex.exec(text)) !== null) {
            if (match.index > lastIndex) {
                parts.push(<span key={`t-${partKey++}`}>{renderCode(text.slice(lastIndex, match.index))}</span>);
            }
            parts.push(<strong key={`b-${partKey++}`} style={{ color: 'var(--text1)', fontWeight: 700 }}>{match[1]}</strong>);
            lastIndex = match.index + match[0].length;
        }
        if (lastIndex < text.length) {
            parts.push(<span key={`t-${partKey++}`}>{renderCode(text.slice(lastIndex))}</span>);
        }
        return parts.length > 0 ? <>{parts}</> : <>{renderCode(text)}</>;
    };

    const renderCode = (text: string): React.ReactNode => {
        // Inline code `text`
        const parts: React.ReactNode[] = [];
        const codeRegex = /`([^`]+)`/g;
        let lastIndex = 0;
        let match;
        let partKey = 0;
        while ((match = codeRegex.exec(text)) !== null) {
            if (match.index > lastIndex) {
                parts.push(text.slice(lastIndex, match.index));
            }
            parts.push(
                <code key={`c-${partKey++}`} style={{
                    padding: '1px 5px',
                    borderRadius: 4,
                    background: 'rgba(255,255,255,0.08)',
                    color: '#93c5fd',
                    fontSize: '0.9em',
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                }}>{match[1]}</code>
            );
            lastIndex = match.index + match[0].length;
        }
        if (lastIndex < text.length) {
            parts.push(text.slice(lastIndex));
        }
        return parts.length > 0 ? <>{parts}</> : <>{text}</>;
    };

    // v3.0.5: Table rendering support
    let tableRows: string[][] = [];
    let tableHeader: string[] = [];
    let tableAligns: ('left' | 'center' | 'right')[] = [];

    const parseTableRow = (row: string): string[] =>
        row.replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());

    const isTableSeparator = (row: string): boolean =>
        /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(row.trim());

    const parseAligns = (row: string): ('left' | 'center' | 'right')[] =>
        parseTableRow(row).map((cell) => {
            const trimmed = cell.trim();
            if (trimmed.startsWith(':') && trimmed.endsWith(':')) return 'center';
            if (trimmed.endsWith(':')) return 'right';
            return 'left';
        });

    const flushTable = () => {
        if (tableHeader.length === 0 && tableRows.length === 0) return;
        const allRows = tableHeader.length > 0 ? [tableHeader, ...tableRows] : tableRows;
        const hasHeader = tableHeader.length > 0;
        elements.push(
            <div key={`tbl-${blockKey++}`} style={{
                margin: '8px 0', borderRadius: 10, overflow: 'hidden',
                border: '1px solid rgba(255,255,255,0.08)',
            }}>
                <table style={{
                    width: '100%', borderCollapse: 'collapse', fontSize: 12, lineHeight: 1.6,
                }}>
                    {hasHeader && (
                        <thead>
                            <tr style={{ background: 'rgba(255,255,255,0.04)' }}>
                                {tableHeader.map((cell, ci) => (
                                    <th key={ci} style={{
                                        padding: '8px 12px', textAlign: tableAligns[ci] || 'left',
                                        fontWeight: 700, color: accent,
                                        borderBottom: `2px solid ${accent}30`,
                                        whiteSpace: 'nowrap',
                                    }}>{renderInline(cell)}</th>
                                ))}
                            </tr>
                        </thead>
                    )}
                    <tbody>
                        {tableRows.map((row, ri) => (
                            <tr key={ri} style={{
                                background: ri % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)',
                            }}>
                                {row.map((cell, ci) => (
                                    <td key={ci} style={{
                                        padding: '6px 12px', textAlign: tableAligns[ci] || 'left',
                                        color: 'var(--text1)',
                                        borderBottom: '1px solid rgba(255,255,255,0.04)',
                                    }}>{renderInline(cell)}</td>
                                ))}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        );
        tableHeader = [];
        tableRows = [];
        tableAligns = [];
    };

    // v3.0.5: Blockquote support
    let quoteLines: string[] = [];
    const flushQuote = () => {
        if (quoteLines.length === 0) return;
        elements.push(
            <div key={`bq-${blockKey++}`} style={{
                margin: '8px 0', padding: '10px 14px', borderRadius: 10,
                borderLeft: `3px solid ${accent}60`,
                background: `${accent}08`,
                color: 'var(--text2)', fontSize: 13, lineHeight: 1.7, fontStyle: 'italic',
            }}>
                {quoteLines.map((ql, qi) => (
                    <div key={qi}>{renderInline(ql)}</div>
                ))}
            </div>
        );
        quoteLines = [];
    };

    for (const line of lines) {
        // Code block
        if (line.trim().startsWith('```')) {
            if (inCodeBlock) {
                flushList();
                flushCode();
                inCodeBlock = false;
            } else {
                flushList();
                inCodeBlock = true;
                codeLang = line.trim().replace('```', '').trim();
            }
            continue;
        }
        if (inCodeBlock) {
            codeLines.push(line);
            continue;
        }

        // v3.0.5: Table rows (| col | col |)
        if (line.trim().match(/^\|(.+\|)+\s*$/)) {
            flushList();
            flushQuote();
            if (isTableSeparator(line)) {
                // This is the separator between header and body
                if (tableRows.length === 1 && tableHeader.length === 0) {
                    tableHeader = tableRows[0];
                    tableRows = [];
                    tableAligns = parseAligns(line);
                }
                continue;
            }
            tableRows.push(parseTableRow(line));
            continue;
        }
        // If we were in a table and hit a non-table line, flush
        if (tableHeader.length > 0 || tableRows.length > 0) {
            flushTable();
        }

        // v3.0.5: Blockquote lines (> text)
        const quoteMatch = line.match(/^>\s?(.*)/);
        if (quoteMatch) {
            flushList();
            quoteLines.push(quoteMatch[1]);
            continue;
        }
        if (quoteLines.length > 0) {
            flushQuote();
        }

        // Headings
        const h1Match = line.match(/^# (.+)/);
        const h2Match = line.match(/^## (.+)/);
        const h3Match = line.match(/^### (.+)/);
        if (h1Match) {
            flushList();
            elements.push(
                <div key={`h1-${blockKey++}`} style={{
                    fontSize: 17, fontWeight: 800, color: accent,
                    margin: '14px 0 6px', borderBottom: `2px solid ${accent}30`, paddingBottom: 6,
                }}>{h1Match[1]}</div>
            );
            continue;
        }
        if (h2Match) {
            flushList();
            elements.push(
                <div key={`h2-${blockKey++}`} style={{
                    fontSize: 15, fontWeight: 700, color: 'var(--text1)',
                    margin: '12px 0 4px',
                }}>{h2Match[1]}</div>
            );
            continue;
        }
        if (h3Match) {
            flushList();
            elements.push(
                <div key={`h3-${blockKey++}`} style={{
                    fontSize: 13, fontWeight: 700, color: 'var(--text2)',
                    margin: '10px 0 3px',
                }}>{h3Match[1]}</div>
            );
            continue;
        }

        // List items
        const listMatch = line.match(/^\s*[-*]\s+(.+)/);
        if (listMatch) {
            listItems.push(listMatch[1]);
            continue;
        }

        // Horizontal rule
        if (line.trim() === '---' || line.trim() === '***') {
            flushList();
            elements.push(
                <hr key={`hr-${blockKey++}`} style={{ border: 'none', borderTop: '1px solid rgba(255,255,255,0.08)', margin: '10px 0' }} />
            );
            continue;
        }

        // Empty line
        if (!line.trim()) {
            flushList();
            continue;
        }

        // Regular paragraph
        flushList();
        elements.push(
            <div key={`p-${blockKey++}`} style={{ fontSize: 13, lineHeight: 1.75, color: 'var(--text1)', marginBottom: 2 }}>
                {renderInline(line)}
            </div>
        );
    }
    flushList();
    flushTable();
    flushQuote();
    if (inCodeBlock) flushCode();

    return <div>{elements}</div>;
}

// v3.0: Role-specific overview description
function roleOverviewHint(nodeType: string, lang: 'en' | 'zh'): string {
    const hints: Record<string, { zh: string; en: string }> = {
        planner: {
            zh: '规划师负责分解用户目标为可执行的子任务方案，包含技术选型、文件架构和视觉设计要求。',
            en: 'The Planner decomposes user goals into executable sub-task plans with tech choices, file architecture, and design requirements.',
        },
        analyst: {
            zh: '分析师负责研究参考资料、搜索相关源码、验证方案可行性，并将研究成果打包传给所有构建者。',
            en: 'The Analyst researches references, searches for relevant source code, validates feasibility, and packages findings for all Builders.',
        },
        builder: {
            zh: '构建者根据规划和分析结果，编写实际代码文件，创建游戏/应用/网站的核心功能。',
            en: 'The Builder writes actual code files based on the plan and analysis, creating core features for games/apps/websites.',
        },
        merger: {
            zh: '合并器负责整合多个构建者的输出，解决文件冲突，优化代码一致性和视觉完整性。',
            en: 'The Merger integrates outputs from multiple Builders, resolves file conflicts, and optimizes code consistency and visual integrity.',
        },
        reviewer: {
            zh: '审查员对产出进行深度质量检查：功能完整性、视觉质量、性能和代码质量。',
            en: 'The Reviewer performs deep quality checks: functional completeness, visual quality, performance, and code quality.',
        },
        imagegen: {
            zh: '图像生成节点使用AI生成项目所需的图片素材、图标、背景等视觉资源。',
            en: 'The Image Gen node uses AI to generate visual assets like images, icons, and backgrounds needed by the project.',
        },
    };
    const hint = hints[nodeType];
    return hint ? (lang === 'zh' ? hint.zh : hint.en) : '';
}

function entryToneColor(type: string): { border: string; bg: string; text: string } {
    if (type === 'error') {
        return { border: 'rgba(255,79,106,0.35)', bg: 'rgba(255,79,106,0.08)', text: '#ff97a9' };
    }
    if (type === 'ok') {
        return { border: 'rgba(64,214,124,0.35)', bg: 'rgba(64,214,124,0.08)', text: '#9ce9bc' };
    }
    if (type === 'sys') {
        return { border: 'rgba(56,189,248,0.35)', bg: 'rgba(56,189,248,0.08)', text: '#9bdcf7' };
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
    currentAction: string,
    workSummary: string[],
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

    if (currentAction) {
        pushEntry({
            ts: endedAt || Date.now(),
            type: status === 'failed' ? 'error' : 'info',
            title: lang === 'zh' ? '当前动作' : 'Current Action',
            text: currentAction,
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

    if (workSummary.length > 0) {
        pushEntry({
            ts: endedAt || Date.now(),
            type: 'ok',
            title: lang === 'zh' ? '已完成工作' : 'Completed Work',
            text: workSummary.map((item, index) => `${index + 1}. ${item}`).join('\n'),
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

    return entries.sort((a, b) => a.ts - b.ts);
}

function artifactMeta(artifactType: string, lang: 'en' | 'zh') {
    const meta = ARTIFACT_TYPE_META[artifactType] || { labelZh: artifactType, labelEn: artifactType, tone: '#94a3b8' };
    return {
        label: lang === 'zh' ? meta.labelZh : meta.labelEn,
        tone: meta.tone,
    };
}

function normalizeArtifactPreview(content: string): string {
    const trimmed = String(content || '').trim();
    if (!trimmed) return '';
    try {
        const parsed = JSON.parse(trimmed);
        return JSON.stringify(parsed, null, 2).slice(0, 4000);
    } catch {
        return trimmed.slice(0, 4000);
    }
}

function mergeNodeSnapshot(nodeData: NonNullable<NodeDetailInput>, canonical?: NodeExecutionRecord | null): NonNullable<NodeDetailInput> {
    if (!canonical) return nodeData;
    const hasOwn = <K extends keyof NodeExecutionRecord>(key: K) =>
        Object.prototype.hasOwnProperty.call(canonical, key);
    const pickString = (value: string | undefined, fallback: string | undefined) =>
        value !== undefined && value !== null ? value : fallback;
    const pickArray = <T,>(key: keyof NodeExecutionRecord, fallback: T[] | undefined): T[] =>
        hasOwn(key) ? ((canonical[key] as T[] | undefined) || []) : (fallback || []);
    const pickObject = <T extends Record<string, unknown>>(key: keyof NodeExecutionRecord, fallback: T | undefined): T =>
        hasOwn(key) ? (((canonical[key] as T | undefined) || {}) as T) : ((fallback || {}) as T);
    const pickLog = () =>
        (hasOwn('activity_log')
            ? ((canonical.activity_log as NonNullable<NodeDetailInput>['log']) || [])
            : (nodeData.log || []));

    // v3.0 fix: Prefer canonical (backend truth) over stale nodeData for data fields.
    // nodeData may contain WebSocket-pushed state from a PREVIOUS run that hasn't been
    // cleared yet. canonical is always the fresh snapshot from the backend.
    return {
        ...nodeData,
        label: pickString(canonical.node_label, nodeData.label),
        nodeType: pickString(canonical.node_key, nodeData.nodeType),
        assignedModel: pickString(canonical.assigned_model, nodeData.assignedModel),
        taskDescription: pickString(canonical.input_summary, nodeData.taskDescription),
        outputSummary: pickString(canonical.output_summary, nodeData.outputSummary),
        lastOutput: pickString(canonical.output_summary, nodeData.lastOutput),
        phase: pickString(canonical.phase, nodeData.phase),
        progress: canonical.progress ?? nodeData.progress,
        // Data fields: canonical takes priority to avoid stale WebSocket pollution
        loadedSkills: pickArray('loaded_skills', nodeData.loadedSkills),
        referenceUrls: pickArray('reference_urls', nodeData.referenceUrls),
        currentAction: pickString(canonical.current_action, nodeData.currentAction),
        workSummary: pickArray('work_summary', nodeData.workSummary),
        toolCallStats: pickObject('tool_call_stats', nodeData.toolCallStats),
        blockingReason: pickString(canonical.blocking_reason, nodeData.blockingReason),
        latestReviewDecision: pickString(canonical.latest_review_decision, nodeData.latestReviewDecision),
        artifactIds: pickArray('artifact_ids', nodeData.artifactIds),
        reportArtifactIds: pickArray('report_artifact_ids', nodeData.reportArtifactIds),
        handoffArtifactIds: pickArray('handoff_artifact_ids', nodeData.handoffArtifactIds),
        dossierArtifactId: pickString(canonical.dossier_artifact_id, nodeData.dossierArtifactId),
        summaryArtifactId: pickString(canonical.summary_artifact_id, nodeData.summaryArtifactId),
        tokensUsed: canonical.tokens_used ?? nodeData.tokensUsed,
        cost: canonical.cost ?? nodeData.cost,
        startedAt: canonical.started_at ?? nodeData.startedAt,
        endedAt: canonical.ended_at ?? nodeData.endedAt,
        modelLatencyMs: canonical.model_latency_ms ?? nodeData.modelLatencyMs,
        log: pickLog(),
    };
}

export default function NodeDetailPopup({ open, onClose, lang, nodeData }: NodeDetailPopupProps) {
    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);
    const [tab, setTab] = useState<DetailTab>('overview');
    const [hydratedNode, setHydratedNode] = useState<NodeExecutionRecord | null>(null);
    const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
    const [loading, setLoading] = useState(false);
    const [expandedArtifactId, setExpandedArtifactId] = useState<string | null>(null);
    const [, setTick] = useState(0);

    const nodeExecutionId = String(nodeData?.nodeExecutionId || '').trim();

    useEffect(() => {
        if (!open || !nodeData || !nodeExecutionId) return;
        let cancelled = false;
        const sync = async () => {
            setLoading(true);
            const [nodeResp, artResp] = await Promise.all([
                getNodeExecution(nodeExecutionId).catch(() => ({ nodeExecution: null as NodeExecutionRecord | null })),
                listArtifacts(undefined, nodeExecutionId).catch(() => ({ artifacts: [] as ArtifactRecord[] })),
            ]);
            if (cancelled) return;
            setHydratedNode(nodeResp.nodeExecution || null);
            setArtifacts(Array.isArray(artResp.artifacts) ? artResp.artifacts : []);
            setLoading(false);
        };
        void sync();
        // V4.3 PERF: Relaxed from 4s to 8s — reduces HTTP requests during execution
        const shouldPoll = String(nodeData.status || '') === 'running';
        const timer = shouldPoll ? window.setInterval(() => { void sync(); }, 8000) : null;
        // v5.0.1: When node reaches terminal status, schedule one delayed re-fetch
        // to catch late-arriving walkthrough report (generated after subtask_complete)
        const isTerminal = ['passed', 'failed', 'cancelled', 'skipped'].includes(String(nodeData.status || ''));
        const delayedFetch = isTerminal ? window.setTimeout(() => { if (!cancelled) void sync(); }, 3500) : null;
        return () => {
            cancelled = true;
            if (timer) window.clearInterval(timer);
            if (delayedFetch) window.clearTimeout(delayedFetch);
        };
    }, [open, nodeData, nodeExecutionId]);

    useEffect(() => {
        if (!open || !nodeData || String(nodeData.status || '') !== 'running') return;
        // V4.3 PERF: Reduced from 1s to 5s
        const timer = window.setInterval(() => setTick((v) => v + 1), 5000);
        return () => window.clearInterval(timer);
    }, [open, nodeData]);

    const sortedArtifacts = useMemo(
        () => [...artifacts].sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0)),
        [artifacts],
    );

    if (!open || !nodeData) return null;

    const handleClose = () => {
        setTab('overview');
        setExpandedArtifactId(null);
        onClose();
    };

    const mergedNode = mergeNodeSnapshot(nodeData, hydratedNode);
    const nodeType = mergedNode.nodeType || 'builder';
    const info = NODE_TYPES[nodeType];
    const status = (mergedNode.status || 'idle') as CanvasNodeStatus;
    const sc = STATUS_COLORS[status] || STATUS_COLORS.idle;
    const accent = info?.color || '#666';
    const label = mergedNode.label || (lang === 'zh' ? info?.label_zh : info?.label_en) || nodeType;
    const progress = Math.max(0, Math.min(100, Number(mergedNode.progress || (status === 'running' ? 5 : 0))));
    const model = mergedNode.assignedModel || mergedNode.model || hydratedNode?.assigned_model || 'gpt-5.4';
    const phase = String(mergedNode.phase || '').trim();
    const phaseLabel = formatPhaseLabel(phase, lang);
    const taskDesc = String(mergedNode.taskDescription || '').trim();
    const outputSummary = String(mergedNode.outputSummary || '').trim();
    const lastOutput = String(mergedNode.lastOutput || '').trim();
    const loadedSkills = Array.isArray(mergedNode.loadedSkills) ? mergedNode.loadedSkills.filter(Boolean) : [];
    const referenceUrls = Array.isArray(mergedNode.referenceUrls) ? mergedNode.referenceUrls.filter(Boolean) : [];
    const workSummary = Array.isArray(mergedNode.workSummary) ? mergedNode.workSummary.filter(Boolean) : [];
    const toolCallStats = mergedNode.toolCallStats || {};
    const currentAction = String(mergedNode.currentAction || '').trim();
    const blockingReason = String(mergedNode.blockingReason || '').trim();
    const latestReviewDecision = String(mergedNode.latestReviewDecision || '').trim();
    const tokensUsed = Number(mergedNode.tokensUsed || 0);
    const promptTokens = Number(mergedNode.promptTokens || 0);
    const completionTokens = Number(mergedNode.completionTokens || 0);
    const cost = Number(mergedNode.cost || 0);
    const startedAt = Number(mergedNode.startedAt || 0);
    const endedAt = Number(mergedNode.endedAt || 0);
    const logs = Array.isArray(mergedNode.log) ? mergedNode.log : [];
    const codeLines = Number((mergedNode as any).codeLines || 0);
    const totalLines = Number((mergedNode as any).totalLines || 0);
    const codeKb = Number((mergedNode as any).codeKb || 0);
    const codeLanguages: string[] = Array.isArray((mergedNode as any).codeLanguages) ? (mergedNode as any).codeLanguages : [];
    const durationText = (() => {
        if (mergedNode.durationSeconds && mergedNode.durationSeconds > 0) {
            const seconds = Number(mergedNode.durationSeconds);
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
        currentAction,
        workSummary,
    );

    const reportArtifacts = sortedArtifacts.filter((artifact) =>
        ['execution_dossier', 'node_summary_report', 'review_rollback_report', 'review_result', 'merge_execution_report', 'deployment_receipt', 'report'].includes(artifact.artifact_type),
    );
    const handoffArtifacts = sortedArtifacts.filter((artifact) =>
        ['source_bundle', 'builder_handoff_report', 'merge_manifest'].includes(artifact.artifact_type),
    );
    const evidenceArtifacts = sortedArtifacts.filter((artifact) =>
        ['browser_capture', 'browser_trace', 'qa_session_capture', 'qa_session_video', 'qa_session_log'].includes(artifact.artifact_type),
    );

    const artifactGroups = [
        { key: 'reports', title: tr('报告', 'Reports'), items: reportArtifacts },
        { key: 'handoffs', title: tr('交接物', 'Handoffs'), items: handoffArtifacts },
        { key: 'evidence', title: tr('测试证据', 'Evidence'), items: evidenceArtifacts },
    ].filter((group) => group.items.length > 0);

    const handleArtifactOpen = async (artifact: ArtifactRecord) => {
        if (typeof window === 'undefined') return;
        const desktopApi = (window as Window & {
            evermind?: {
                openPath?: (targetPath: string) => Promise<boolean> | boolean;
                revealInFinder?: (targetPath: string) => Promise<boolean> | boolean;
            };
        }).evermind;
        if (artifact.path && desktopApi?.openPath) {
            const opened = await desktopApi.openPath(artifact.path);
            if (opened) return;
        }
        if (artifact.path && desktopApi?.revealInFinder) {
            await desktopApi.revealInFinder(artifact.path);
        }
    };

    return (
        <div
            onClick={handleClose}
            style={{
                position: 'fixed',
                inset: 0,
                zIndex: 9999,
                background: 'rgba(0,0,0,0.6)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: 12,
            }}
        >
            <div
                onClick={(e) => e.stopPropagation()}
                style={{
                    width: 980,
                    maxWidth: 'calc(100vw - 24px)',
                    maxHeight: '88vh',
                    overflow: 'hidden',
                    background: 'var(--surface-strong, #111827)',
                    border: `1.5px solid ${accent}2f`,
                    borderRadius: 22,
                    boxShadow: `0 28px 90px rgba(0,0,0,0.55), 0 0 32px ${accent}16`,
                    display: 'flex',
                    flexDirection: 'column',
                }}
            >
                <div
                    style={{
                        padding: '18px 20px 14px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 14,
                        borderBottom: `1px solid ${accent}24`,
                        background: `linear-gradient(135deg, ${accent}18, rgba(255,255,255,0.02))`,
                    }}
                >
                    <div
                        style={{
                            width: 42,
                            height: 42,
                            borderRadius: 14,
                            display: 'inline-flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            fontSize: 13,
                            fontWeight: 800,
                            color: accent,
                            background: `${accent}20`,
                            border: `1px solid ${accent}35`,
                            flexShrink: 0,
                        }}
                    >
                        {info?.icon || nodeType.slice(0, 2).toUpperCase()}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                            <div style={{ fontSize: 17, fontWeight: 800, color: 'var(--text1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {label}
                            </div>
                            <span
                                style={{
                                    padding: '4px 10px',
                                    borderRadius: 999,
                                    fontSize: 11,
                                    fontWeight: 800,
                                    color: sc.color,
                                    background: `${sc.color}18`,
                                    border: `1px solid ${sc.color}36`,
                                }}
                            >
                                {lang === 'zh' ? sc.labelZh : sc.labelEn}
                            </span>
                            {phaseLabel ? (
                                <span
                                    style={{
                                        padding: '4px 10px',
                                        borderRadius: 999,
                                        fontSize: 11,
                                        fontWeight: 700,
                                        color: '#cbd5e1',
                                        background: 'rgba(255,255,255,0.05)',
                                        border: '1px solid rgba(255,255,255,0.08)',
                                    }}
                                >
                                    {phaseLabel}
                                </span>
                            ) : null}
                            {latestReviewDecision ? (
                                <span
                                    style={{
                                        padding: '4px 10px',
                                        borderRadius: 999,
                                        fontSize: 11,
                                        fontWeight: 700,
                                        color: latestReviewDecision.includes('reject') ? '#ff97a9' : '#9ce9bc',
                                        background: latestReviewDecision.includes('reject') ? 'rgba(255,79,106,0.12)' : 'rgba(64,214,124,0.12)',
                                        border: latestReviewDecision.includes('reject') ? '1px solid rgba(255,79,106,0.28)' : '1px solid rgba(64,214,124,0.28)',
                                    }}
                                >
                                    {tr('审查结论', 'Review')}: {latestReviewDecision}
                                </span>
                            ) : null}
                        </div>
                        <div style={{ marginTop: 6, display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, color: 'var(--text3)' }}>
                            <span>{nodeTypeLabel(nodeType, lang)}</span>
                            <span>{tr('模型', 'Model')}: {model}</span>
                            <span>{tr('耗时', 'Duration')}: {durationText}</span>
                            <span>{tr('进度', 'Progress')}: {progress}%</span>
                            {nodeExecutionId ? <span>ID: {nodeExecutionId}</span> : null}
                            {loading ? <span>{tr('同步中', 'Syncing')}</span> : null}
                        </div>
                    </div>
                    <button
                        onClick={handleClose}
                        style={{
                            border: '1px solid rgba(255,255,255,0.08)',
                            background: 'rgba(255,255,255,0.04)',
                            color: 'var(--text2)',
                            borderRadius: 12,
                            padding: '8px 12px',
                            cursor: 'pointer',
                        }}
                    >
                        {tr('关闭', 'Close')}
                    </button>
                </div>

                <div
                    style={{
                        display: 'flex',
                        gap: 8,
                        padding: '12px 16px 0',
                        borderBottom: '1px solid rgba(255,255,255,0.06)',
                    }}
                >
                    {([
                        ['overview', tr('概览', 'Overview')],
                        ['timeline', tr('执行轨迹', 'Timeline')],
                        ['refs', tr('技能与参考', 'Skills & Refs')],
                        ['reports', tr('报告与产物', 'Reports & Artifacts')],
                    ] as Array<[DetailTab, string]>).map(([tabKey, tabLabel]) => {
                        const active = tab === tabKey;
                        return (
                            <button
                                key={tabKey}
                                onClick={() => setTab(tabKey)}
                                style={{
                                    border: 'none',
                                    background: active ? `${accent}20` : 'transparent',
                                    color: active ? accent : 'var(--text3)',
                                    borderBottom: active ? `2px solid ${accent}` : '2px solid transparent',
                                    padding: '10px 14px',
                                    fontSize: 12,
                                    fontWeight: 800,
                                    cursor: 'pointer',
                                }}
                            >
                                {tabLabel}
                            </button>
                        );
                    })}
                </div>

                <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
                    {tab === 'overview' ? (
                        <div style={{ display: 'grid', gap: 14 }}>
                            <div
                                style={{
                                    display: 'grid',
                                    gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
                                    gap: 10,
                                }}
                            >
                                {[
                                    [tr('Token', 'Tokens'), formatTokens(tokensUsed)],
                                    [tr('输入 Token', 'Prompt'), formatTokens(promptTokens)],
                                    [tr('输出 Token', 'Completion'), formatTokens(completionTokens)],
                                    [tr('成本', 'Cost'), formatCost(cost)],
                                    [tr('开始', 'Started'), formatClock(startedAt, lang)],
                                    [tr('结束', 'Ended'), endedAt ? formatClock(endedAt, lang) : (status === 'running' ? tr('进行中', 'Running') : '—')],
                                ].map(([title, value]) => (
                                    <div
                                        key={String(title)}
                                        style={{
                                            padding: '12px 14px',
                                            borderRadius: 14,
                                            border: '1px solid rgba(255,255,255,0.08)',
                                            background: 'rgba(255,255,255,0.03)',
                                        }}
                                    >
                                        <div style={{ fontSize: 11, color: 'var(--text3)', marginBottom: 6 }}>{title}</div>
                                        <div style={{ fontSize: 15, fontWeight: 800, color: 'var(--text1)' }}>{value}</div>
                                    </div>
                                ))}
                            </div>

                            {/* v3.0: Token usage progress bar */}
                            {tokensUsed > 0 ? (
                                <div style={{
                                    padding: '10px 14px',
                                    borderRadius: 14,
                                    border: '1px solid rgba(255,255,255,0.08)',
                                    background: 'rgba(255,255,255,0.03)',
                                }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                                        <span style={{ fontSize: 11, color: 'var(--text3)' }}>{tr('Token 使用进度', 'Token Usage')}</span>
                                        <span style={{ fontSize: 11, color: 'var(--text3)' }}>
                                            {formatTokens(tokensUsed)} / 128K
                                        </span>
                                    </div>
                                    <div style={{
                                        height: 6,
                                        borderRadius: 3,
                                        background: 'rgba(255,255,255,0.06)',
                                        overflow: 'hidden',
                                    }}>
                                        <div style={{
                                            height: '100%',
                                            borderRadius: 3,
                                            background: `linear-gradient(90deg, ${accent}, ${accent}88)`,
                                            width: `${Math.min(100, (tokensUsed / 128000) * 100)}%`,
                                            transition: 'width 0.5s ease',
                                        }} />
                                    </div>
                                </div>
                            ) : null}

                            {/* v3.0.3: Code output statistics panel */}
                            {codeLines > 0 ? (
                                <div style={{
                                    padding: '14px 16px',
                                    borderRadius: 14,
                                    border: '1px solid rgba(167,139,250,0.2)',
                                    background: 'linear-gradient(135deg, rgba(167,139,250,0.08), rgba(255,255,255,0.02))',
                                }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                                        <span style={{ fontSize: 16 }}>📝</span>
                                        <span style={{ fontSize: 12, fontWeight: 800, color: '#a78bfa' }}>
                                            {tr('代码输出统计', 'Code Output Statistics')}
                                        </span>
                                    </div>
                                    <div style={{
                                        display: 'grid',
                                        gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))',
                                        gap: 8,
                                    }}>
                                        <div style={{
                                            padding: '8px 10px', borderRadius: 10,
                                            background: 'rgba(167,139,250,0.06)',
                                            border: '1px solid rgba(167,139,250,0.12)',
                                        }}>
                                            <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 4 }}>
                                                {tr('代码行数', 'Code Lines')}
                                            </div>
                                            <div style={{ fontSize: 18, fontWeight: 800, color: '#a78bfa' }}>
                                                {codeLines.toLocaleString()}
                                            </div>
                                        </div>
                                        <div style={{
                                            padding: '8px 10px', borderRadius: 10,
                                            background: 'rgba(167,139,250,0.06)',
                                            border: '1px solid rgba(167,139,250,0.12)',
                                        }}>
                                            <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 4 }}>
                                                {tr('总行数', 'Total Lines')}
                                            </div>
                                            <div style={{ fontSize: 18, fontWeight: 800, color: 'var(--text1)' }}>
                                                {totalLines.toLocaleString()}
                                            </div>
                                        </div>
                                        <div style={{
                                            padding: '8px 10px', borderRadius: 10,
                                            background: 'rgba(167,139,250,0.06)',
                                            border: '1px solid rgba(167,139,250,0.12)',
                                        }}>
                                            <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 4 }}>
                                                {tr('代码体积', 'Code Size')}
                                            </div>
                                            <div style={{ fontSize: 18, fontWeight: 800, color: 'var(--text1)' }}>
                                                {codeKb}KB
                                            </div>
                                        </div>
                                    </div>
                                    {codeLanguages.length > 0 && (
                                        <div style={{ display: 'flex', gap: 6, marginTop: 10, flexWrap: 'wrap' }}>
                                            {codeLanguages.map((lang_tag) => (
                                                <span key={lang_tag} style={{
                                                    padding: '2px 8px', borderRadius: 999, fontSize: 10, fontWeight: 600,
                                                    background: 'rgba(167,139,250,0.12)',
                                                    color: '#c4b5fd',
                                                    border: '1px solid rgba(167,139,250,0.2)',
                                                }}>{lang_tag}</span>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            ) : null}

                            {/* v3.0: Role-specific overview hint */}
                            {roleOverviewHint(nodeType, lang) ? (
                                <div style={{
                                    padding: '12px 14px',
                                    borderRadius: 14,
                                    border: '1px solid rgba(255,255,255,0.06)',
                                    background: 'rgba(255,255,255,0.02)',
                                    fontSize: 12,
                                    color: 'var(--text3)',
                                    lineHeight: 1.65,
                                    fontStyle: 'italic',
                                }}>
                                    {roleOverviewHint(nodeType, lang)}
                                </div>
                            ) : null}

                            <div
                                style={{
                                    padding: 16,
                                    borderRadius: 16,
                                    border: `1px solid ${accent}28`,
                                    background: `linear-gradient(135deg, ${accent}12, rgba(255,255,255,0.03))`,
                                }}
                            >
                                <div style={{ fontSize: 12, fontWeight: 800, color: accent, marginBottom: 8 }}>{tr('当前正在做什么', 'Current Work')}</div>
                                <div style={{ fontSize: 14, lineHeight: 1.65, color: 'var(--text1)', whiteSpace: 'pre-wrap' }}>
                                    {currentAction || currentWork || tr('暂无实时动作描述。', 'No live action summary yet.')}
                                </div>
                            </div>

                            <div
                                style={{
                                    padding: 16,
                                    borderRadius: 16,
                                    border: '1px solid rgba(255,255,255,0.08)',
                                    background: 'rgba(255,255,255,0.03)',
                                }}
                            >
                                <div style={{ fontSize: 12, fontWeight: 800, color: 'var(--text2)', marginBottom: 10 }}>{tr('已完成的工作', 'Completed Work')}</div>
                                {workSummary.length > 0 ? (
                                    <div style={{ display: 'grid', gap: 8 }}>
                                        {workSummary.map((item, index) => (
                                            <div
                                                key={`${item}-${index}`}
                                                style={{
                                                    borderRadius: 12,
                                                    border: '1px solid rgba(255,255,255,0.08)',
                                                    background: 'rgba(255,255,255,0.025)',
                                                    padding: '10px 12px',
                                                    fontSize: 13,
                                                    color: 'var(--text1)',
                                                    lineHeight: 1.6,
                                                }}
                                            >
                                                <span style={{ color: accent, fontWeight: 800, marginRight: 8 }}>
                                                    {tr('完成', 'Done')} {index + 1}
                                                </span>
                                                {item}
                                            </div>
                                        ))}
                                    </div>
                                ) : (
                                    <div style={{ fontSize: 13, color: 'var(--text3)' }}>
                                        {outputSummary || tr('节点尚未生成结构化总结。', 'No structured summary available yet.')}
                                    </div>
                                )}
                            </div>

                            {blockingReason ? (
                                <div
                                    style={{
                                        padding: 16,
                                        borderRadius: 16,
                                        border: '1px solid rgba(255,79,106,0.24)',
                                        background: 'rgba(255,79,106,0.08)',
                                    }}
                                >
                                    <div style={{ fontSize: 12, fontWeight: 800, color: '#ff97a9', marginBottom: 8 }}>{tr('阻塞 / 打回原因', 'Blocking Reason')}</div>
                                    <div style={{ fontSize: 13, lineHeight: 1.65, color: '#ffd1d9', whiteSpace: 'pre-wrap' }}>{blockingReason}</div>
                                </div>
                            ) : null}
                        </div>
                    ) : null}

                    {tab === 'timeline' ? (
                        <div style={{ display: 'grid', gap: 10 }}>
                            {/* v3.0: Tool Call Stats Bar */}
                            {Object.keys(toolCallStats).length > 0 ? (
                                <div style={{
                                    padding: 14,
                                    borderRadius: 16,
                                    border: `1px solid ${accent}22`,
                                    background: `linear-gradient(135deg, ${accent}08, rgba(255,255,255,0.02))`,
                                }}>
                                    <div style={{ fontSize: 11, fontWeight: 800, color: accent, marginBottom: 10, textTransform: 'uppercase', letterSpacing: 0.5 }}>
                                        {tr('工具调用轨迹', 'Tool Call Trace')}
                                    </div>
                                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                                        {Object.entries(toolCallStats)
                                            .sort(([, a], [, b]) => (b as number) - (a as number))
                                            .map(([key, value]) => {
                                                const icon = toolIcon(key);
                                                return (
                                                    <div key={key} style={{
                                                        display: 'flex', alignItems: 'center', gap: 6,
                                                        padding: '6px 10px',
                                                        borderRadius: 10,
                                                        border: '1px solid rgba(255,255,255,0.08)',
                                                        background: 'rgba(255,255,255,0.04)',
                                                    }}>
                                                        <span style={{ fontSize: 14 }}>{icon}</span>
                                                        <span style={{ fontSize: 12, color: 'var(--text2)', fontWeight: 600 }}>{key}</span>
                                                        <span style={{
                                                            fontSize: 11, fontWeight: 800, color: accent,
                                                            padding: '1px 6px', borderRadius: 999,
                                                            background: `${accent}18`,
                                                        }}>{String(value)}</span>
                                                    </div>
                                                );
                                            })}
                                    </div>
                                </div>
                            ) : null}

                            {/* v3.0: Enhanced Timeline Entries */}
                            {timelineEntries.length > 0 ? timelineEntries.map((entry, index) => {
                                const tone = entryToneColor(entry.type);
                                const detectedTool = extractToolMention(entry.text);
                                const isLast = index === timelineEntries.length - 1;
                                const prevTs = index > 0 ? timelineEntries[index - 1].ts : 0;
                                const elapsed = prevTs > 0 ? Math.round((entry.ts - prevTs) / 1000) : 0;

                                return (
                                    <div key={`${entry.ts}-${index}`} style={{ position: 'relative' }}>
                                        {/* Connector line */}
                                        {index > 0 ? (
                                            <div style={{
                                                position: 'absolute', left: 18, top: -10,
                                                width: 2, height: 10,
                                                background: `linear-gradient(180deg, ${tone.border}, transparent)`,
                                            }} />
                                        ) : null}
                                        <div style={{
                                            borderRadius: 16,
                                            border: `1px solid ${tone.border}`,
                                            background: tone.bg,
                                            padding: 14,
                                            transition: 'all 0.2s ease',
                                        }}>
                                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 8 }}>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                                    {/* Tool icon */}
                                                    {detectedTool ? (
                                                        <span style={{ fontSize: 16, opacity: 0.9 }}>{toolIcon(detectedTool)}</span>
                                                    ) : (
                                                        <span style={{
                                                            width: 8, height: 8, borderRadius: '50%',
                                                            background: tone.text, display: 'inline-block',
                                                            boxShadow: isLast && status === 'running' ? `0 0 6px ${tone.text}` : 'none',
                                                            animation: isLast && status === 'running' ? 'pulse 1.5s infinite' : 'none',
                                                        }} />
                                                    )}
                                                    <div style={{ fontSize: 12, fontWeight: 800, color: tone.text }}>
                                                        {entry.title || tr('执行事件', 'Execution Event')}
                                                    </div>
                                                </div>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                                    {elapsed > 0 ? (
                                                        <span style={{
                                                            fontSize: 10, color: 'var(--text3)',
                                                            padding: '2px 6px', borderRadius: 999,
                                                            background: 'rgba(255,255,255,0.04)',
                                                            fontWeight: 600,
                                                        }}>+{elapsed}s</span>
                                                    ) : null}
                                                    <div style={{ fontSize: 11, color: 'var(--text3)' }}>{formatClock(entry.ts, lang)}</div>
                                                </div>
                                            </div>
                                            <div style={{ fontSize: 13, lineHeight: 1.7, color: 'var(--text1)', whiteSpace: 'pre-wrap' }}>
                                                {entry.text}
                                            </div>
                                        </div>
                                    </div>
                                );
                            }) : (
                                <div style={{
                                    padding: 40, textAlign: 'center', borderRadius: 16,
                                    border: '1px solid rgba(255,255,255,0.06)', background: 'rgba(255,255,255,0.02)',
                                }}>
                                    <div style={{ fontSize: 28, marginBottom: 8, opacity: 0.4 }}>⏳</div>
                                    <div style={{ fontSize: 13, color: 'var(--text3)' }}>
                                        {tr('暂无执行轨迹，节点尚未开始处理。', 'No timeline available. Node has not started processing yet.')}
                                    </div>
                                </div>
                            )}
                        </div>
                    ) : null}

                    {tab === 'refs' ? (
                        <div style={{ display: 'grid', gap: 14 }}>
                            <div
                                style={{
                                    padding: 16,
                                    borderRadius: 16,
                                    border: '1px solid rgba(255,255,255,0.08)',
                                    background: 'rgba(255,255,255,0.03)',
                                }}
                            >
                                <div style={{ fontSize: 12, fontWeight: 800, color: 'var(--text2)', marginBottom: 10 }}>{tr('技能 / Skills', 'Skills')}</div>
                                {loadedSkills.length > 0 ? (
                                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                                        {loadedSkills.map((skill) => (
                                            <span
                                                key={skill}
                                                style={{
                                                    padding: '7px 10px',
                                                    borderRadius: 999,
                                                    border: '1px solid rgba(255,255,255,0.08)',
                                                    background: 'rgba(255,255,255,0.04)',
                                                    fontSize: 12,
                                                    color: 'var(--text2)',
                                                }}
                                            >
                                                {formatSkillLabel(skill)}
                                            </span>
                                        ))}
                                    </div>
                                ) : (
                                    <div style={{ fontSize: 13, color: 'var(--text3)' }}>{tr('未记录技能。', 'No skills recorded.')}</div>
                                )}
                            </div>

                            <div
                                style={{
                                    padding: 16,
                                    borderRadius: 16,
                                    border: '1px solid rgba(255,255,255,0.08)',
                                    background: 'rgba(255,255,255,0.03)',
                                }}
                            >
                                <div style={{ fontSize: 12, fontWeight: 800, color: 'var(--text2)', marginBottom: 10 }}>{tr('参考网站 / References', 'References')}</div>
                                {referenceUrls.length > 0 ? (
                                    <div style={{ display: 'grid', gap: 8 }}>
                                        {referenceUrls.map((url) => (
                                            <a
                                                key={url}
                                                href={url}
                                                target="_blank"
                                                rel="noreferrer"
                                                style={{
                                                    display: 'block',
                                                    padding: '10px 12px',
                                                    borderRadius: 12,
                                                    border: '1px solid rgba(255,255,255,0.08)',
                                                    background: 'rgba(255,255,255,0.025)',
                                                    color: '#93c5fd',
                                                    textDecoration: 'none',
                                                    fontSize: 13,
                                                    wordBreak: 'break-all',
                                                }}
                                            >
                                                {url}
                                            </a>
                                        ))}
                                    </div>
                                ) : (
                                    <div style={{ fontSize: 13, color: 'var(--text3)' }}>{tr('未记录外部参考。', 'No external references captured.')}</div>
                                )}
                            </div>

                            <div
                                style={{
                                    padding: 16,
                                    borderRadius: 16,
                                    border: '1px solid rgba(255,255,255,0.08)',
                                    background: 'rgba(255,255,255,0.03)',
                                }}
                            >
                                <div style={{ fontSize: 12, fontWeight: 800, color: 'var(--text2)', marginBottom: 10 }}>{tr('工具调用统计', 'Tool Call Stats')}</div>
                                {Object.keys(toolCallStats).length > 0 ? (
                                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                                        {Object.entries(toolCallStats).map(([key, value]) => (
                                            <span
                                                key={key}
                                                style={{
                                                    padding: '7px 10px',
                                                    borderRadius: 999,
                                                    border: '1px solid rgba(255,255,255,0.08)',
                                                    background: 'rgba(255,255,255,0.04)',
                                                    fontSize: 12,
                                                    color: 'var(--text2)',
                                                }}
                                            >
                                                {key}: {value}
                                            </span>
                                        ))}
                                    </div>
                                ) : (
                                    <div style={{ fontSize: 13, color: 'var(--text3)' }}>{tr('暂无工具统计。', 'No tool stats recorded.')}</div>
                                )}
                            </div>
                        </div>
                    ) : null}

                    {tab === 'reports' ? (
                        <div style={{ display: 'grid', gap: 16 }}>
                            {/* v3.0: Walkthrough Report section */}
                            {hydratedNode && (hydratedNode as any).walkthrough_report ? (
                                <div>
                                    <div style={{ fontSize: 13, fontWeight: 800, color: accent, marginBottom: 10 }}>
                                        {tr('📋 任务报告 Walkthrough', '📋 Task Walkthrough Report')}
                                    </div>
                                    <div style={{
                                        borderRadius: 16,
                                        border: `1px solid ${accent}28`,
                                        background: `linear-gradient(135deg, ${accent}08, rgba(255,255,255,0.02))`,
                                        overflow: 'hidden',
                                    }}>
                                        <div style={{
                                            padding: 16,
                                            maxHeight: 600,
                                            overflowY: 'auto',
                                        }}>
                                            <SimpleMarkdown content={(hydratedNode as any).walkthrough_report} accentColor={accent} />
                                        </div>
                                    </div>
                                </div>
                            ) : null}

                            {/* v3.0: Files produced by this node */}
                            {hydratedNode && (hydratedNode as any).files_created?.length > 0 ? (
                                <div>
                                    <div style={{ fontSize: 13, fontWeight: 800, color: 'var(--text2)', marginBottom: 10 }}>
                                        {tr('📁 产出文件', '📁 Files Produced')}
                                    </div>
                                    <div style={{ display: 'grid', gap: 6 }}>
                                        {((hydratedNode as any).files_created as string[]).map((file: string, i: number) => (
                                            <div key={`${file}-${i}`} style={{
                                                padding: '8px 12px',
                                                borderRadius: 10,
                                                border: '1px solid rgba(64,214,124,0.15)',
                                                background: 'rgba(64,214,124,0.05)',
                                                fontSize: 12,
                                                color: '#9ce9bc',
                                                fontFamily: 'monospace',
                                            }}>
                                                ✨ {file}
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            ) : null}

                            {artifactGroups.length > 0 ? artifactGroups.map((group) => (
                                <div key={group.key}>
                                    <div style={{ fontSize: 13, fontWeight: 800, color: 'var(--text2)', marginBottom: 10 }}>{group.title}</div>
                                    <div style={{ display: 'grid', gap: 10 }}>
                                        {group.items.map((artifact) => {
                                            const meta = artifactMeta(artifact.artifact_type, lang);
                                            const expanded = expandedArtifactId === artifact.id;
                                            return (
                                                <div
                                                    key={artifact.id}
                                                    style={{
                                                        borderRadius: 16,
                                                        border: '1px solid rgba(255,255,255,0.08)',
                                                        background: 'rgba(255,255,255,0.03)',
                                                        overflow: 'hidden',
                                                    }}
                                                >
                                                    <div style={{ padding: 14 }}>
                                                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
                                                            <div style={{ minWidth: 0 }}>
                                                                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 6 }}>
                                                                    <span
                                                                        style={{
                                                                            padding: '4px 8px',
                                                                            borderRadius: 999,
                                                                            fontSize: 11,
                                                                            fontWeight: 800,
                                                                            color: meta.tone,
                                                                            background: `${meta.tone}18`,
                                                                            border: `1px solid ${meta.tone}2f`,
                                                                        }}
                                                                    >
                                                                        {meta.label}
                                                                    </span>
                                                                    <span style={{ fontSize: 11, color: 'var(--text3)' }}>
                                                                        {formatArtifactTs(artifact.created_at, lang)}
                                                                    </span>
                                                                </div>
                                                                <div style={{ fontSize: 14, fontWeight: 800, color: 'var(--text1)', wordBreak: 'break-word' }}>
                                                                    {artifact.title || artifact.artifact_type}
                                                                </div>
                                                                {artifact.path ? (
                                                                    <div style={{ fontSize: 11, color: 'var(--text3)', marginTop: 6, wordBreak: 'break-all' }}>
                                                                        {artifact.path}
                                                                    </div>
                                                                ) : null}
                                                            </div>
                                                            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                                                                {artifact.path ? (
                                                                    <button
                                                                        onClick={() => void handleArtifactOpen(artifact)}
                                                                        style={{
                                                                            border: '1px solid rgba(255,255,255,0.08)',
                                                                            background: 'rgba(255,255,255,0.04)',
                                                                            color: 'var(--text2)',
                                                                            borderRadius: 10,
                                                                            padding: '8px 10px',
                                                                            fontSize: 12,
                                                                            cursor: 'pointer',
                                                                        }}
                                                                    >
                                                                        {tr('打开文件', 'Open File')}
                                                                    </button>
                                                                ) : null}
                                                                <button
                                                                    onClick={() => setExpandedArtifactId(expanded ? null : artifact.id)}
                                                                    style={{
                                                                        border: `1px solid ${accent}28`,
                                                                        background: `${accent}14`,
                                                                        color: accent,
                                                                        borderRadius: 10,
                                                                        padding: '8px 10px',
                                                                        fontSize: 12,
                                                                        cursor: 'pointer',
                                                                    }}
                                                                >
                                                                    {expanded ? tr('收起内容', 'Hide') : tr('查看内容', 'Preview')}
                                                                </button>
                                                            </div>
                                                        </div>
                                                    </div>
                                                    {expanded ? (
                                                        <div
                                                            style={{
                                                                borderTop: '1px solid rgba(255,255,255,0.06)',
                                                                background: 'rgba(0,0,0,0.18)',
                                                                padding: 14,
                                                            }}
                                                        >
                                                            <pre
                                                                style={{
                                                                    margin: 0,
                                                                    whiteSpace: 'pre-wrap',
                                                                    wordBreak: 'break-word',
                                                                    fontSize: 12,
                                                                    lineHeight: 1.65,
                                                                    color: 'var(--text2)',
                                                                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                                                                }}
                                                            >
                                                                {normalizeArtifactPreview(artifact.content) || tr('该产物没有内联内容，请直接打开文件查看。', 'No inline content stored. Open the file instead.')}
                                                            </pre>
                                                        </div>
                                                    ) : null}
                                                </div>
                                            );
                                        })}
                                    </div>
                                </div>
                            )) : null}

                            {!artifactGroups.length && !(hydratedNode && (hydratedNode as any).walkthrough_report) ? (
                                <div style={{ fontSize: 13, color: 'var(--text3)' }}>{tr('当前节点还没有报告或产物。', 'No reports or artifacts yet.')}</div>
                            ) : null}
                        </div>
                    ) : null}
                </div>
            </div>
        </div>
    );
}
