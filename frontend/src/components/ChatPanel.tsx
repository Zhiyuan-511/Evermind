'use client';

import { ChatAttachment, ChatMessage } from '@/lib/types';
import { uploadChatAttachments } from '@/lib/api';
import { dedupeChatAttachments, formatAttachmentSize, MAX_CHAT_ATTACHMENTS } from '@/lib/chatAttachments';
import { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import TaskSummaryStrip from './TaskSummaryStrip';
import RunCompletionCard from './RunCompletionCard';

interface ChatPanelProps {
    messages: ChatMessage[];
    onSendGoal: (goal: string, attachments?: ChatAttachment[]) => void;
    sessionId?: string;
    connected: boolean;
    running: boolean;
    onStop: () => void;
    lang: 'en' | 'zh';
    difficulty: 'simple' | 'standard' | 'pro' | 'ultra' | 'custom';
    onDifficultyChange: (d: 'simple' | 'standard' | 'pro' | 'ultra' | 'custom') => void;
    cliEnabled?: boolean;  // v7.1i: Ultra requires CLI mode on, otherwise dimmed/disabled
    customCanvasNodeCount?: number;
    runtimeMode?: string;
    taskTitle?: string | null;
    taskStatus?: string | null;
    activeNodeLabels?: string[];
    completedNodes?: number;
    runningNodes?: number;
    totalNodes?: number;
    startedAt?: number | null;
    endedAt?: number | null;
    onOpenReports?: () => void;
    onRevealInFinder?: (previewUrl: string) => void;
    selectedRuntime?: 'local' | 'openclaw';
    onRuntimeChange?: (runtime: 'local' | 'openclaw') => void;
    showOpenClawRuntime?: boolean;
}

// ── Sanitization (unchanged) ──
const ALLOWED_TAGS = new Set(['b', 'strong', 'i', 'em', 'br', 'code', 'span', 'div', 'a']);
const SAFE_LINK_PROTOCOLS = new Set(['http:', 'https:']);

function sanitizeHref(rawHref: string | null): string | null {
    if (!rawHref) return null;
    const trimmed = rawHref.trim();
    if (!trimmed) return null;
    try {
        const parsed = new URL(trimmed, 'http://127.0.0.1');
        if (!SAFE_LINK_PROTOCOLS.has(parsed.protocol)) return null;
        return parsed.href;
    } catch {
        return null;
    }
}

function sanitizeHtmlWithRegexFallback(html: string): string {
    return html
        .replace(/\s+on\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]*)/gi, '')
        .replace(/<\s*script[\s>][\s\S]*?<\s*\/\s*script\s*>/gi, '')
        .replace(/<\s*style[\s>][\s\S]*?<\s*\/\s*style\s*>/gi, '')
        .replace(/\b(href|src)\s*=\s*["']?\s*javascript:/gi, '$1="')
        .replace(/<\/?([a-z][a-z0-9]*)\b[^>]*>/gi, (match, tag) => {
            return ALLOWED_TAGS.has(tag.toLowerCase()) ? match : '';
        });
}

function sanitizeHtml(html: string): string {
    if (!html) return '';
    if (typeof window === 'undefined' || typeof DOMParser === 'undefined') {
        return sanitizeHtmlWithRegexFallback(html);
    }
    const parser = new DOMParser();
    const sourceDoc = parser.parseFromString(`<div>${html}</div>`, 'text/html');
    const sourceRoot = sourceDoc.body.firstElementChild;
    if (!sourceRoot) return '';
    const outputDoc = document.implementation.createHTMLDocument('');
    const outputRoot = outputDoc.createElement('div');
    const sanitizeNode = (node: Node, parent: HTMLElement) => {
        if (node.nodeType === Node.TEXT_NODE) {
            parent.appendChild(outputDoc.createTextNode(node.textContent || ''));
            return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        const el = node as HTMLElement;
        const tag = el.tagName.toLowerCase();
        if (!ALLOWED_TAGS.has(tag)) {
            Array.from(el.childNodes).forEach((child) => sanitizeNode(child, parent));
            return;
        }
        if (tag === 'a') {
            const safeHref = sanitizeHref(el.getAttribute('href'));
            if (!safeHref) {
                Array.from(el.childNodes).forEach((child) => sanitizeNode(child, parent));
                return;
            }
            const link = outputDoc.createElement('a');
            link.setAttribute('href', safeHref);
            if (el.getAttribute('target') === '_blank') link.setAttribute('target', '_blank');
            link.setAttribute('rel', 'noopener noreferrer');
            Array.from(el.childNodes).forEach((child) => sanitizeNode(child, link));
            parent.appendChild(link);
            return;
        }
        const safeEl = outputDoc.createElement(tag);
        Array.from(el.childNodes).forEach((child) => sanitizeNode(child, safeEl));
        parent.appendChild(safeEl);
    };
    Array.from(sourceRoot.childNodes).forEach((child) => sanitizeNode(child, outputRoot));
    return outputRoot.innerHTML;
}

// ── Milestone detection: messages that belong in the Execution Feed ──
const MILESTONE_SENDERS = new Set(['Orchestrator', 'Plan', 'File Output']);
const MILESTONE_ICON_SET = new Set(['🧠', '📋', '✅', '❌', '⚙️', '📁', '🔍', '🏁']);

function isMilestone(msg: ChatMessage): boolean {
    if (msg.role === 'user') return true;
    if (msg.sender === 'console') return false;
    if (msg.sender === 'OpenClaw' || msg.icon === 'OC') return true;
    if (MILESTONE_SENDERS.has(msg.sender || '')) return true;
    if (msg.icon && MILESTONE_ICON_SET.has(msg.icon)) return true;
    // Subtask start/complete messages
    const content = msg.content || '';
    if (/^(✅|❌|⚙️)\s/.test(content)) return true;
    return false;
}

function stripLeadingMarker(content: string): string {
    return content
        .replace(/^[\u2700-\u27BF\u{1F300}-\u{1FAFF}\u{1F1E6}-\u{1F1FF}]+\s*/u, '')
        .trim();
}

function messageBadge(msg: ChatMessage, lang: 'en' | 'zh'): { label: string; color: string; bg: string } {
    if (msg.role === 'user') {
        return { label: lang === 'zh' ? '目标' : 'Goal', color: 'var(--blue)', bg: 'rgba(91, 140, 255, 0.1)' };
    }
    if (msg.icon === '✅' || msg.icon === '🏁') {
        return { label: lang === 'zh' ? '完成' : 'Done', color: '#22c55e', bg: 'rgba(34, 197, 94, 0.1)' };
    }
    if (msg.icon === '❌') {
        return { label: lang === 'zh' ? '失败' : 'Failed', color: '#ef4444', bg: 'rgba(239, 68, 68, 0.1)' };
    }
    if (msg.icon === '⚙️') {
        return { label: lang === 'zh' ? '执行中' : 'Running', color: 'var(--blue)', bg: 'rgba(91, 140, 255, 0.1)' };
    }
    if (msg.sender === 'Plan' || msg.icon === '📋') {
        return { label: lang === 'zh' ? '规划' : 'Plan', color: '#a855f7', bg: 'rgba(168, 85, 247, 0.1)' };
    }
    if (msg.sender === 'File Output') {
        return { label: lang === 'zh' ? '产物' : 'Output', color: '#22c55e', bg: 'rgba(34, 197, 94, 0.08)' };
    }
    if (msg.sender === 'OpenClaw') {
        return { label: 'OC', color: '#a855f7', bg: 'rgba(168, 85, 247, 0.1)' };
    }
    if (msg.sender === 'Orchestrator' || msg.sender === 'Evermind') {
        return { label: lang === 'zh' ? '系统' : 'System', color: '#3b82f6', bg: 'rgba(59, 130, 246, 0.08)' };
    }
    if (msg.sender === 'Report' || msg.sender === 'Preview') {
        return { label: lang === 'zh' ? '报告' : 'Report', color: '#06b6d4', bg: 'rgba(6, 182, 212, 0.08)' };
    }
    // P1-3: Agent-specific role badges
    const sLower = (msg.sender || '').toLowerCase();
    if (sLower.includes('builder'))   return { label: lang === 'zh' ? '编写' : 'Build', color: '#22c55e', bg: 'rgba(34, 197, 94, 0.08)' };
    if (sLower.includes('reviewer'))  return { label: lang === 'zh' ? '审核' : 'Review', color: '#f59e0b', bg: 'rgba(245, 158, 11, 0.08)' };
    if (sLower.includes('tester'))    return { label: lang === 'zh' ? '测试' : 'Test', color: '#06b6d4', bg: 'rgba(6, 182, 212, 0.08)' };
    if (sLower.includes('deployer'))  return { label: lang === 'zh' ? '部署' : 'Deploy', color: '#10b981', bg: 'rgba(16, 185, 129, 0.08)' };
    if (sLower.includes('validator')) return { label: lang === 'zh' ? '验证' : 'Validate', color: '#a855f7', bg: 'rgba(168, 85, 247, 0.08)' };
    if (sLower.includes('planner'))   return { label: lang === 'zh' ? '规划' : 'Plan', color: '#a855f7', bg: 'rgba(168, 85, 247, 0.08)' };
    return { label: lang === 'zh' ? '事件' : 'Event', color: 'var(--text2)', bg: 'var(--glass)' };
}

// ── Message card style by type ──
function getCardStyle(msg: ChatMessage): React.CSSProperties {
    if (msg.role === 'user') {
        return {
            background: 'rgba(91, 140, 255, 0.08)',
            borderLeft: '3px solid var(--blue)',
            borderRadius: 8,
        };
    }
    // Success milestones
    if (msg.icon === '✅' || msg.icon === '🏁') {
        return {
            background: 'rgba(34, 197, 94, 0.06)',
            borderLeft: '3px solid #22c55e',
            borderRadius: 8,
        };
    }
    // Failure milestones
    if (msg.icon === '❌') {
        return {
            background: 'rgba(239, 68, 68, 0.06)',
            borderLeft: '3px solid #ef4444',
            borderRadius: 8,
        };
    }
    // Running milestones
    if (msg.icon === '⚙️') {
        return {
            background: 'rgba(91, 140, 255, 0.04)',
            borderLeft: '3px solid rgba(91, 140, 255, 0.4)',
            borderRadius: 8,
        };
    }
    // Plan/orchestrator
    if (msg.icon === '🧠' || msg.icon === '📋') {
        return {
            background: 'rgba(168, 85, 247, 0.05)',
            borderLeft: '3px solid rgba(168, 85, 247, 0.4)',
            borderRadius: 8,
        };
    }
    return {
        borderLeft: '3px solid var(--glass-border)',
        borderRadius: 8,
    };
}

function isNearBottom(element: HTMLDivElement, threshold = 48): boolean {
    return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function scrollToBottom(element: HTMLDivElement): void {
    element.scrollTop = element.scrollHeight;
}

function AttachmentList({
    attachments,
    lang,
    onRemove,
}: {
    attachments: ChatAttachment[];
    lang: 'en' | 'zh';
    onRemove?: (attachmentId: string) => void;
}) {
    if (!attachments.length) return null;
    return (
        <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(88px, 1fr))',
            gap: 8,
            marginTop: 8,
        }}>
            {attachments.map((attachment) => {
                const isImage = attachment.kind === 'image' && !!attachment.previewUrl;
                return (
                    <div
                        key={attachment.id}
                        title={attachment.path}
                        style={{
                            position: 'relative',
                            borderRadius: 10,
                            border: '1px solid rgba(255,255,255,0.08)',
                            background: 'rgba(255,255,255,0.04)',
                            overflow: 'hidden',
                            minHeight: isImage ? 112 : 72,
                        }}
                    >
                        {onRemove && (
                            <button
                                type="button"
                                onClick={() => onRemove(attachment.id)}
                                style={{
                                    position: 'absolute',
                                    top: 4,
                                    right: 4,
                                    zIndex: 2,
                                    width: 20,
                                    height: 20,
                                    borderRadius: '50%',
                                    border: 'none',
                                    cursor: 'pointer',
                                    background: 'rgba(0,0,0,0.55)',
                                    color: '#fff',
                                    fontSize: 11,
                                    lineHeight: '20px',
                                }}
                            >
                                ×
                            </button>
                        )}
                        {isImage ? (
                            <a
                                href={attachment.previewUrl}
                                target="_blank"
                                rel="noopener noreferrer"
                                style={{ display: 'block', textDecoration: 'none' }}
                            >
                                {/* eslint-disable-next-line @next/next/no-img-element */}
                                <img
                                    src={attachment.previewUrl}
                                    alt={attachment.name}
                                    style={{
                                        display: 'block',
                                        width: '100%',
                                        height: 84,
                                        objectFit: 'cover',
                                        background: 'rgba(0,0,0,0.18)',
                                    }}
                                />
                            </a>
                        ) : (
                            <div style={{
                                height: 52,
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                fontSize: 18,
                                color: 'var(--text2)',
                                background: 'rgba(255,255,255,0.03)',
                            }}>
                                {attachment.kind === 'image' ? '🖼' : '📎'}
                            </div>
                        )}
                        <div style={{ padding: '7px 8px 8px' }}>
                            <div style={{
                                fontSize: 10,
                                fontWeight: 600,
                                color: 'var(--text1)',
                                lineHeight: 1.35,
                                wordBreak: 'break-word',
                            }}>
                                {attachment.name}
                            </div>
                            <div style={{
                                marginTop: 4,
                                fontSize: 9,
                                color: 'var(--text3)',
                                lineHeight: 1.3,
                            }}>
                                {formatAttachmentSize(attachment.size)}
                                {attachment.kind === 'image'
                                    ? ` · ${lang === 'zh' ? '图片' : 'Image'}`
                                    : ` · ${lang === 'zh' ? '文件' : 'File'}`}
                            </div>
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

export default function ChatPanel({
    messages,
    onSendGoal,
    sessionId = '',
    connected,
    running,
    onStop,
    lang,
    difficulty,
    onDifficultyChange,
    runtimeMode,
    taskTitle,
    taskStatus,
    activeNodeLabels = [],
    completedNodes = 0,
    runningNodes = 0,
    totalNodes = 0,
    startedAt,
    endedAt,
    onOpenReports,
    onRevealInFinder,
    selectedRuntime = 'local',
    onRuntimeChange,
    showOpenClawRuntime = true,
    customCanvasNodeCount = 0,
    cliEnabled = false,
}: ChatPanelProps) {
    const [input, setInput] = useState('');
    const [logsExpanded, setLogsExpanded] = useState(false);
    const [inputFocused, setInputFocused] = useState(false);
    const [sendFlash, setSendFlash] = useState(false);
    const [pendingAttachments, setPendingAttachments] = useState<ChatAttachment[]>([]);
    const [attachmentsBusy, setAttachmentsBusy] = useState(false);
    const [attachmentError, setAttachmentError] = useState('');
    const feedRef = useRef<HTMLDivElement>(null);
    const logsRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLInputElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const feedShouldAutoScrollRef = useRef(true);
    const logsShouldAutoScrollRef = useRef(true);

    const tr = useCallback((zh: string, en: string) => lang === 'zh' ? zh : en, [lang]);

    // P0-1: Auto-focus input on mount and when run finishes
    useEffect(() => {
        if (!running && inputRef.current) {
            inputRef.current.focus();
        }
    }, [running]);

    // v6.2 (maintainer 2026-04-20): listen for template pre-fill events dispatched
    // by WelcomeWizard / TemplateGallery. Fills the input with the template's
    // goal so the user just needs to hit Send.
    useEffect(() => {
        const handler = (e: Event) => {
            const detail = (e as CustomEvent).detail as { goal?: string } | undefined;
            const goal = String(detail?.goal || '').trim();
            if (!goal) return;
            setInput(goal);
            setTimeout(() => inputRef.current?.focus(), 50);
        };
        window.addEventListener('evermind-prefill-goal', handler);
        return () => window.removeEventListener('evermind-prefill-goal', handler);
    }, []);

    // Split messages into feed (milestones) and logs (console)
    const feedMessages = useMemo(
        () => messages.filter(m => isMilestone(m)),
        [messages]
    );
    const logMessages = useMemo(
        () => messages.filter(m => m.sender === 'console'),
        [messages]
    );
    const latestFeedMessageId = feedMessages[feedMessages.length - 1]?.id || '';
    const latestLogMessageId = logMessages[logMessages.length - 1]?.id || '';

    const handleFeedScroll = useCallback(() => {
        if (!feedRef.current) return;
        feedShouldAutoScrollRef.current = isNearBottom(feedRef.current);
    }, []);

    const handleLogsScroll = useCallback(() => {
        if (!logsRef.current) return;
        logsShouldAutoScrollRef.current = isNearBottom(logsRef.current);
    }, []);

    const handlePickAttachments = useCallback(() => {
        if (attachmentsBusy) return;
        fileInputRef.current?.click();
    }, [attachmentsBusy]);

    const handleAttachmentSelection = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(event.target.files || []).slice(0, MAX_CHAT_ATTACHMENTS);
        event.target.value = '';
        if (files.length === 0) return;
        setAttachmentsBusy(true);
        setAttachmentError('');
        try {
            const { attachments, rejected } = await uploadChatAttachments(sessionId, files);
            if (attachments.length > 0) {
                setPendingAttachments((prev) => dedupeChatAttachments([...prev, ...attachments]).slice(0, MAX_CHAT_ATTACHMENTS));
            }
            if (rejected.length > 0) {
                setAttachmentError(rejected.slice(0, 2).join(' · '));
            }
        } catch (error) {
            setAttachmentError(String(error instanceof Error ? error.message : error || tr('附件上传失败', 'Attachment upload failed')));
        } finally {
            setAttachmentsBusy(false);
        }
    }, [sessionId, tr]);

    const handleRemoveAttachment = useCallback((attachmentId: string) => {
        setPendingAttachments((prev) => prev.filter((attachment) => attachment.id !== attachmentId));
        setAttachmentError('');
    }, []);

    useEffect(() => {
        if (!feedRef.current || !feedShouldAutoScrollRef.current) return;
        scrollToBottom(feedRef.current);
    }, [feedMessages.length, latestFeedMessageId]);

    useEffect(() => {
        if (!logsRef.current || !logsExpanded || !logsShouldAutoScrollRef.current) return;
        scrollToBottom(logsRef.current);
    }, [logMessages.length, latestLogMessageId, logsExpanded]);

    // P0-2: Send with visual flash confirmation
    const handleSend = useCallback(() => {
        if ((!input.trim() && pendingAttachments.length === 0) || !connected || attachmentsBusy) return;
        onSendGoal(input.trim(), pendingAttachments);
        setInput('');
        setPendingAttachments([]);
        setAttachmentError('');
        setSendFlash(true);
        setTimeout(() => setSendFlash(false), 800);
    }, [attachmentsBusy, connected, input, onSendGoal, pendingAttachments]);

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };

    return (
        <div className="glass-strong flex flex-col h-full border-l border-white/5" style={{ width: '100%', minWidth: 0, flexShrink: 0, overflow: 'hidden' }}>

            {/* Layer 1: TaskSummaryStrip */}
            <TaskSummaryStrip
                running={running}
                lang={lang}
                messages={messages}
                runtimeMode={runtimeMode}
                taskTitle={taskTitle}
                taskStatus={taskStatus}
                activeNodeLabels={activeNodeLabels}
                completedNodes={completedNodes}
                runningNodes={runningNodes}
                totalNodes={totalNodes}
                startedAt={startedAt}
                endedAt={endedAt}
            />

            {/* Layer 2: Execution Feed */}
            <div ref={feedRef} onScroll={handleFeedScroll} className="flex-1 overflow-y-auto p-3 space-y-2" style={{ minHeight: 0 }}>
                {feedMessages.length === 0 ? (
                    <div className="text-center py-8 text-[var(--text3)] text-[11px]">
                        <div className="font-medium mb-1">{tr('发送一个目标', 'Send a goal')}</div>
                        <div className="text-[9px]">{tr('AI 会自动规划、编写、测试', 'AI will auto-plan, code, and test')}</div>
                    </div>
                ) : (
                    feedMessages.map(msg => {
                        const badge = messageBadge(msg, lang);
                        const completionPreviewUrl = msg.completionData?.previewUrl;
                        return (
                        <div
                            key={msg.id}
                            style={{
                                ...getCardStyle(msg),
                                padding: '8px 10px',
                                transition: 'all 0.15s',
                            }}
                        >
                            <div style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: 6,
                                marginBottom: 2,
                            }}>
                                <span style={{
                                    fontSize: 9,
                                    fontWeight: 700,
                                    color: badge.color,
                                    background: badge.bg,
                                    borderRadius: 999,
                                    padding: '2px 6px',
                                    lineHeight: 1.2,
                                }}>
                                    {badge.label}
                                </span>
                                <span style={{
                                    fontSize: 10,
                                    fontWeight: 600,
                                    color: msg.borderColor || 'var(--text2)',
                                }}>
                                    {msg.role === 'user' ? tr('你', 'You') : (msg.sender || msg.role)}
                                </span>
                                <span style={{
                                    fontSize: 8,
                                    color: 'var(--text3)',
                                    marginLeft: 'auto',
                                }}>
                                    {msg.timestamp}
                                </span>
                            </div>
                            {msg.completionData ? (
                                <RunCompletionCard
                                    {...msg.completionData}
                                    lang={lang}
                                    onOpenReports={onOpenReports}
                                    onRevealInFinder={completionPreviewUrl
                                        ? () => onRevealInFinder?.(completionPreviewUrl)
                                        : undefined}
                                />
                            ) : (
                            <div
                                style={{
                                    fontSize: 11,
                                    color: msg.role === 'user' ? 'var(--text1)' : 'var(--text2)',
                                    lineHeight: 1.5,
                                    wordBreak: 'break-word',
                                }}
                                dangerouslySetInnerHTML={{ __html: sanitizeHtml(stripLeadingMarker(msg.content)) }}
                            />
                            )}
                            {msg.attachments && msg.attachments.length > 0 && (
                                <AttachmentList attachments={msg.attachments} lang={lang} />
                            )}
                        </div>
                    )})
                )}
            </div>

            {/* Layer 3: Collapsible Raw Log */}
            <div style={{ borderTop: '1px solid var(--glass-border)' }}>
                <button
                    onClick={() => setLogsExpanded(!logsExpanded)}
                    style={{
                        width: '100%',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                        padding: '6px 12px',
                        fontSize: 10,
                        fontWeight: 600,
                        color: 'var(--text3)',
                        background: logsExpanded ? 'rgba(255,255,255,0.02)' : 'transparent',
                        border: 'none',
                        cursor: 'pointer',
                        transition: 'all 0.15s',
                        textAlign: 'left',
                    }}
                >
                    <span style={{
                        transform: logsExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                        transition: 'transform 0.15s',
                        display: 'inline-block',
                        fontSize: 8,
                    }}>▶</span>
                    <span>{tr('诊断日志', 'Diagnostic Logs')}</span>
                    <span style={{
                        background: 'var(--glass)',
                        borderRadius: 8,
                        padding: '0 5px',
                        fontSize: 9,
                        marginLeft: 'auto',
                    }}>
                        {logMessages.length}
                    </span>
                </button>

                {logsExpanded && (
                    <div
                        ref={logsRef}
                        onScroll={handleLogsScroll}
                        style={{
                            maxHeight: 180,
                            overflow: 'auto',
                            padding: '4px 8px',
                            fontSize: 9,
                            fontFamily: 'monospace',
                            background: 'rgba(0,0,0,0.15)',
                        }}
                    >
                        {logMessages.map(msg => (
                            <div key={msg.id} style={{
                                padding: '2px 0',
                                color: 'var(--text3)',
                                borderBottom: '1px solid rgba(255,255,255,0.03)',
                                wordBreak: 'break-all',
                                lineHeight: 1.4,
                            }}>
                                {msg.content}
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {/* Input area */}
            <div className="p-3 border-t border-white/5">
                {running && (
                    <button onClick={onStop} style={{
                        width: '100%',
                        marginBottom: 8,
                        padding: '6px 0',
                        fontSize: 10,
                        fontWeight: 600,
                        color: '#ef4444',
                        background: 'rgba(239, 68, 68, 0.08)',
                        border: '1px solid rgba(239, 68, 68, 0.2)',
                        borderRadius: 8,
                        cursor: 'pointer',
                        transition: 'all 0.15s',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        gap: 4,
                    }}>
                        {tr('停止执行', 'Stop execution')}
                    </button>
                )}

                {/* Difficulty selector — v7.1i (maintainer 2026-04-25): 5 modes incl. Ultra.
                    Ultra mode triggers 14-NE pro plan + Ultra CLI orchestration:
                    analyst + uidesign + scribe + 4 parallel builders + merger + polisher
                    + reviewer + patcher + deployer + tester + debugger. */}
                <div style={{
                    display: 'flex', gap: 2, marginBottom: 8,
                    borderRadius: 8, overflow: 'hidden',
                    border: '1px solid var(--glass-border)',
                }}>
                    {(() => {
                        const tiers: Array<[string, string, string]> = [
                            ['simple', tr('极速', 'Blitz'), tr('2-3 个节点', '2-3 nodes')],
                            ['standard', tr('平衡', 'Balanced'), tr('3-4 个节点', '3-4 nodes')],
                            ['pro', tr('深度', 'Deep'), tr('7-10 个节点', '7-10 nodes')],
                        ];
                        // v7.1i (maintainer 2026-04-25): Ultra 按钮只在 CLI 模式开启时显示。
                        // 否则按钮根本不出现在 UI 上 —— Ultra 依赖 CLI 进程，没 CLI 这选项没意义。
                        if (cliEnabled) {
                            tiers.push(['ultra', tr('Ultra', 'Ultra'),
                                tr('14 节点 + 4 并行 builder', '14 nodes + 4 parallel builders')]);
                        }
                        tiers.push(['custom', tr('自定义', 'Custom'),
                            customCanvasNodeCount > 0
                                ? tr(`${customCanvasNodeCount} 个画布节点`, `${customCanvasNodeCount} canvas nodes`)
                                : tr('先在画布上拖节点', 'Arrange nodes on canvas first'),
                        ]);
                        return tiers.map(([key, label, hint]) => {
                        const isCustom = key === 'custom';
                        const customMissing = isCustom && customCanvasNodeCount < 2;
                        // v7.1i: Ultra requires CLI mode to be enabled.
                        // When CLI mode is OFF, Ultra is dimmed and disabled.
                        const isUltra = key === 'ultra';
                        const ultraDisabled = isUltra && !cliEnabled;
                        // v7.2 (maintainer 2026-04-26): Custom mode is no longer a
                        // hard-disabled button when the canvas is empty —
                        // selecting it without ≥2 nodes simply shows an inline
                        // hint banner so users know to drag nodes in. Hard
                        // disabling led to confusion ("the lock icon, can't
                        // click it"). Ultra still hard-disables when CLI off.
                        const disabled = ultraDisabled;
                        const tooltip = ultraDisabled
                            ? tr('需先在设置中开启 CLI 模式', 'Enable CLI Mode in Settings first')
                            : customMissing
                                ? tr('点击选择，再去画布拖 ≥2 个节点（点击后会显示提示）', 'Click to select; then drag ≥2 nodes onto the canvas (a hint will appear).')
                                : hint;
                        return (
                            <button
                                key={key}
                                disabled={disabled}
                                onClick={() => {
                                    if (!disabled) {
                                        onDifficultyChange(key as 'simple' | 'standard' | 'pro' | 'ultra' | 'custom');
                                    }
                                }}
                                title={tooltip}
                                style={{
                                    flex: 1, padding: '5px 0',
                                    fontSize: 10, fontWeight: 600,
                                    border: 'none',
                                    cursor: disabled ? 'not-allowed' : 'pointer',
                                    background: difficulty === key
                                        ? key === 'simple' ? 'rgba(91,140,255,0.12)'
                                        : key === 'standard' ? 'rgba(255,154,64,0.12)'
                                        : key === 'pro' ? 'rgba(168,85,247,0.12)'
                                        : key === 'ultra' ? 'rgba(236,72,153,0.14)'
                                        : 'rgba(34,197,94,0.12)'
                                        : 'transparent',
                                    color: difficulty === key
                                        ? key === 'simple' ? 'var(--blue)'
                                        : key === 'standard' ? 'var(--orange)'
                                        : key === 'pro' ? 'var(--purple)'
                                        : key === 'ultra' ? '#ec4899'
                                        : '#22c55e'
                                        : (customMissing || ultraDisabled ? 'var(--text3)' : 'var(--text3)'),
                                    opacity: ultraDisabled ? 0.35
                                        : (customMissing && difficulty === 'custom' ? 0.6 : 1),
                                    transition: 'all 0.15s',
                                }}
                            >
                                {label}
                                {ultraDisabled && <span style={{ marginLeft: 3, fontSize: 9 }}>🔒</span>}
                            </button>
                        );
                        });
                    })()}
                </div>
                {difficulty === 'custom' && (
                    <div style={{
                        marginBottom: 8, padding: '6px 8px',
                        fontSize: 10, lineHeight: 1.5,
                        color: customCanvasNodeCount >= 2 ? '#22c55e' : 'var(--orange)',
                        background: customCanvasNodeCount >= 2 ? 'rgba(34,197,94,0.06)' : 'rgba(255,154,64,0.06)',
                        border: `1px solid ${customCanvasNodeCount >= 2 ? 'rgba(34,197,94,0.25)' : 'rgba(255,154,64,0.25)'}`,
                        borderRadius: 6,
                    }}>
                        {customCanvasNodeCount >= 2
                            ? tr(
                                `✓ 自定义模式：画布当前有 ${customCanvasNodeCount} 个节点，发送任务后 Planner 会严格按画布拓扑分派。`,
                                `✓ Custom mode: ${customCanvasNodeCount} nodes on canvas. Planner will strictly follow your canvas topology when you send the task.`
                            )
                            : tr(
                                '⚠ 自定义模式：画布上节点不足（需 ≥ 2 个）。请在右侧画布拖入节点或从模板库加载，或切回其他模式。',
                                '⚠ Custom mode: not enough nodes on canvas (need ≥ 2). Drag nodes on the canvas at right, load a template, or switch back to another mode.'
                            )}
                    </div>
                )}
                {/* P1-2: Runtime toggle */}
                {showOpenClawRuntime && onRuntimeChange && (
                    <div style={{
                        display: 'flex', gap: 0, marginBottom: 6,
                        borderRadius: 8, overflow: 'hidden',
                        border: '1px solid var(--glass-border)',
                    }}>
                        {([['local', tr('本地执行', 'Local'), 'var(--blue)'],
                           ['openclaw', tr('OpenClaw', 'OpenClaw'), '#a855f7']] as const).map(([key, label, color]) => (
                            <button
                                key={key}
                                onClick={() => onRuntimeChange(key as 'local' | 'openclaw')}
                                style={{
                                    flex: 1, padding: '4px 0',
                                    fontSize: 10, fontWeight: 600,
                                    border: 'none', cursor: 'pointer',
                                    background: selectedRuntime === key ? `${color}1a` : 'transparent',
                                    color: selectedRuntime === key ? color : 'var(--text3)',
                                    transition: 'all 0.15s',
                                    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
                                }}
                            >
                                <span style={{
                                    width: 6,
                                    height: 6,
                                    borderRadius: '50%',
                                    background: selectedRuntime === key ? color : 'var(--text4)',
                                    opacity: selectedRuntime === key ? 1 : 0.65,
                                    flexShrink: 0,
                                }} />
                                {label}
                            </button>
                        ))}
                    </div>
                )}
                {showOpenClawRuntime && selectedRuntime === 'openclaw' && (
                    <div style={{
                        fontSize: 9,
                        color: 'var(--text3)',
                        marginBottom: 8,
                        padding: '6px 8px',
                        borderRadius: 8,
                        background: 'rgba(168, 85, 247, 0.06)',
                        border: '1px solid rgba(168, 85, 247, 0.14)',
                        lineHeight: 1.45,
                    }}>
                        {tr(
                            'Direct Mode：任务将直接通过 OpenClaw 派发节点执行，进度自动同步到桌面。',
                            'Direct Mode: nodes are dispatched directly via OpenClaw. Progress syncs to desktop automatically.',
                        )}
                    </div>
                )}

                <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    onChange={handleAttachmentSelection}
                    style={{ display: 'none' }}
                />

                {(pendingAttachments.length > 0 || attachmentError) && (
                    <div style={{
                        marginBottom: 8,
                        padding: '8px 9px',
                        borderRadius: 10,
                        border: '1px solid rgba(255,255,255,0.08)',
                        background: 'rgba(255,255,255,0.03)',
                    }}>
                        {pendingAttachments.length > 0 && (
                            <>
                                <div style={{
                                    fontSize: 9,
                                    fontWeight: 700,
                                    color: 'var(--text3)',
                                    letterSpacing: '0.04em',
                                    textTransform: 'uppercase',
                                }}>
                                    {tr('待发送附件', 'Pending attachments')}
                                </div>
                                <AttachmentList attachments={pendingAttachments} lang={lang} onRemove={handleRemoveAttachment} />
                            </>
                        )}
                        {attachmentError && (
                            <div style={{
                                marginTop: pendingAttachments.length > 0 ? 8 : 0,
                                fontSize: 9,
                                color: '#f59e0b',
                                lineHeight: 1.45,
                            }}>
                                {attachmentError}
                            </div>
                        )}
                    </div>
                )}

                <div className="flex gap-2" style={{ position: 'relative', zIndex: 10 }}>
                    <button
                        type="button"
                        onClick={handlePickAttachments}
                        disabled={!connected || attachmentsBusy}
                        title={tr('添加文件或图片', 'Add files or images')}
                        style={{
                            width: 34,
                            flexShrink: 0,
                            fontSize: 15,
                            background: !connected || attachmentsBusy ? 'rgba(255,255,255,0.04)' : 'rgba(255,255,255,0.06)',
                            color: !connected || attachmentsBusy ? 'var(--text3)' : 'var(--text1)',
                            border: '1px solid rgba(255,255,255,0.1)',
                            borderRadius: 8,
                            cursor: !connected || attachmentsBusy ? 'not-allowed' : 'pointer',
                            transition: 'all 0.2s',
                        }}
                    >
                        {attachmentsBusy ? '...' : '+'}
                    </button>
                    <input
                        ref={inputRef}
                        value={input}
                        onChange={e => setInput(e.target.value)}
                        onKeyDown={handleKeyDown}
                        onFocus={() => setInputFocused(true)}
                        onBlur={() => setInputFocused(false)}
                        disabled={!connected}
                        autoFocus
                        placeholder={connected
                            ? tr('输入目标，如: 创建一个登录页面...', 'Enter goal: Build a login page...')
                            : tr('后端未连接...', 'Backend not connected...')}
                        style={{
                            flex: 1,
                            background: sendFlash ? 'rgba(34, 197, 94, 0.12)' : !connected ? 'rgba(255,255,255,0.02)' : 'rgba(255,255,255,0.05)',
                            border: inputFocused ? '2px solid var(--blue)' : '1px solid rgba(255,255,255,0.1)',
                            borderRadius: 8,
                            padding: inputFocused ? '7px 11px' : '8px 12px',
                            fontSize: 11,
                            color: !connected ? 'var(--text3)' : 'var(--text1)',
                            outline: 'none',
                            transition: 'all 0.2s ease',
                            boxShadow: inputFocused ? '0 0 0 3px rgba(91, 140, 255, 0.15)' : sendFlash ? '0 0 12px rgba(34, 197, 94, 0.3)' : 'none',
                        }}
                    />
                    <button
                        onClick={handleSend}
                        disabled={!connected || attachmentsBusy || (!input.trim() && pendingAttachments.length === 0)}
                        style={{
                            padding: '6px 14px',
                            fontSize: 14,
                            background: !connected || attachmentsBusy || (!input.trim() && pendingAttachments.length === 0) ? 'rgba(255,255,255,0.04)' : sendFlash ? 'rgba(34, 197, 94, 0.2)' : 'rgba(91, 140, 255, 0.12)',
                            color: !connected || attachmentsBusy || (!input.trim() && pendingAttachments.length === 0) ? 'var(--text3)' : sendFlash ? '#22c55e' : 'var(--blue)',
                            border: `1px solid ${sendFlash ? 'rgba(34,197,94,0.3)' : 'rgba(91, 140, 255, 0.2)'}`,
                            borderRadius: 8,
                            cursor: !connected || attachmentsBusy || (!input.trim() && pendingAttachments.length === 0) ? 'not-allowed' : 'pointer',
                            transition: 'all 0.2s',
                            fontWeight: 600,
                            transform: sendFlash ? 'scale(0.92)' : 'scale(1)',
                        }}
                    >
                        {sendFlash ? '✓' : '→'}
                    </button>
                </div>
                <div style={{
                    fontSize: 8,
                    color: 'var(--text3)',
                    marginTop: 6,
                    textAlign: 'center',
                }}>
                    {connected
                        ? <>
                            <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#22c55e', display: 'inline-block', marginRight: 4 }} />
                            {tr('自主模式 — AI 将自动计划、执行、测试', 'Autonomous — AI will plan, execute, test')}
                          </>
                        : <>
                            <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#ef4444', display: 'inline-block', marginRight: 4 }} />
                            {tr('离线 — 启动后端: python server.py', 'Offline — start backend: python server.py')}
                          </>
                    }
                </div>
                <div style={{
                    fontSize: 8,
                    color: 'var(--text3)',
                    marginTop: 4,
                    textAlign: 'center',
                    opacity: 0.85,
                }}>
                    {tr('支持拖选图片、PDF、文档等常见文件作为任务上下文', 'Attach images, PDFs, docs, and other common files as task context')}
                </div>
            </div>
        </div>
    );
}
