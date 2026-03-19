'use client';

import { ChatMessage } from '@/lib/types';
import { useState, useRef, useEffect } from 'react';

interface ChatPanelProps {
    messages: ChatMessage[];
    onSendGoal: (goal: string) => void;
    connected: boolean;
    running: boolean;
    onStop: () => void;
    lang: 'en' | 'zh';
    difficulty: 'simple' | 'standard' | 'pro';
    onDifficultyChange: (d: 'simple' | 'standard' | 'pro') => void;
}

// Lightweight HTML sanitizer with safe anchor support for preview links
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
        if (node.nodeType !== Node.ELEMENT_NODE) {
            return;
        }

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
            if (el.getAttribute('target') === '_blank') {
                link.setAttribute('target', '_blank');
            }
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

export default function ChatPanel({ messages, onSendGoal, connected, running, onStop, lang, difficulty, onDifficultyChange }: ChatPanelProps) {
    const [input, setInput] = useState('');
    const [tab, setTab] = useState<'chat' | 'console'>('chat');
    const msgsRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (msgsRef.current) msgsRef.current.scrollTop = msgsRef.current.scrollHeight;
    }, [messages]);

    const handleSend = () => {
        if (!input.trim()) return;
        onSendGoal(input.trim());
        setInput('');
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };

    return (
        <div className="glass-strong flex flex-col h-full border-l border-white/5" style={{ width: '320px', minWidth: 0, overflow: 'hidden' }}>
            {/* Tabs */}
            <div className="flex border-b border-white/5">
                <button
                    className={`flex-1 py-2 text-[11px] font-medium transition-colors ${tab === 'chat' ? 'text-[var(--blue)] border-b-2 border-[var(--blue)]' : 'text-[var(--text3)] hover:text-[var(--text2)]'}`}
                    onClick={() => setTab('chat')}
                >
                    💬 {lang === 'zh' ? '任务' : 'Tasks'}
                </button>
                <button
                    className={`flex-1 py-2 text-[11px] font-medium transition-colors ${tab === 'console' ? 'text-[var(--blue)] border-b-2 border-[var(--blue)]' : 'text-[var(--text3)] hover:text-[var(--text2)]'}`}
                    onClick={() => setTab('console')}
                >
                    📋 {lang === 'zh' ? '日志' : 'Console'}
                </button>
            </div>

            {/* Messages area */}
            <div ref={msgsRef} className="flex-1 overflow-y-auto p-3 space-y-2">
                {tab === 'chat' ? (
                    messages.filter(m => m.role !== 'system' || m.sender !== 'console').length === 0 ? (
                        <div className="text-center py-8 text-[var(--text3)] text-[11px]">
                            <div className="text-3xl mb-2">🧠</div>
                            <div className="font-medium mb-1">{lang === 'zh' ? '发送一个目标' : 'Send a goal'}</div>
                            <div className="text-[9px]">{lang === 'zh' ? 'AI 会自动规划、编写、测试' : 'AI will auto-plan, code, and test'}</div>
                        </div>
                    ) : (
                        messages.filter(m => m.sender !== 'console').map(msg => (
                            <div key={msg.id} className={`chat-msg ${msg.role}`} style={msg.borderColor ? { borderLeft: `2px solid ${msg.borderColor}` } : {}}>
                                <div className="chat-sender">{msg.icon} {msg.sender || msg.role}</div>
                                <div dangerouslySetInnerHTML={{ __html: sanitizeHtml(msg.content) }} />
                                <div className="chat-time">{msg.timestamp}</div>
                            </div>
                        ))
                    )
                ) : (
                    /* Console tab */
                    messages.filter(m => m.sender === 'console').map(msg => (
                        <div key={msg.id} className={`log-line ${msg.role}`}>
                            <span className="log-tag">{msg.sender}</span>
                            <span>{msg.content}</span>
                        </div>
                    ))
                )}
            </div>

            {/* Difficulty selector + Input area */}
            <div className="p-3 border-t border-white/5">
                {running && (
                    <button onClick={onStop} className="btn btn-danger w-full mb-2 text-[10px] justify-center">
                        ⏹ {lang === 'zh' ? '停止执行' : 'Stop execution'}
                    </button>
                )}

                {/* Difficulty selector */}
                <div style={{
                    display: 'flex', gap: 2, marginBottom: 8,
                    borderRadius: 8, overflow: 'hidden',
                    border: '1px solid var(--glass-border)',
                }}>
                    {([['simple', '⚡', lang === 'zh' ? '极速' : 'Blitz', '2-3'],
                       ['standard', '🔥', lang === 'zh' ? '平衡' : 'Balanced', '3-4'],
                       ['pro', '💎', lang === 'zh' ? '深度' : 'Deep', '5-7']] as const).map(([key, icon, label, nodes]) => (
                        <button
                            key={key}
                            onClick={() => onDifficultyChange(key as 'simple' | 'standard' | 'pro')}
                            title={`${nodes} ${lang === 'zh' ? '个节点' : 'nodes'}`}
                            style={{
                                flex: 1, padding: '5px 0',
                                fontSize: 10, fontWeight: 600,
                                border: 'none', cursor: 'pointer',
                                background: difficulty === key
                                    ? key === 'simple' ? 'rgba(79,143,255,0.12)'
                                    : key === 'standard' ? 'rgba(255,154,64,0.12)'
                                    : 'rgba(168,85,247,0.12)'
                                    : 'transparent',
                                color: difficulty === key
                                    ? key === 'simple' ? 'var(--blue)'
                                    : key === 'standard' ? 'var(--orange)'
                                    : 'var(--purple)'
                                    : 'var(--text3)',
                                transition: 'all 0.15s',
                            }}
                        >
                            {icon} {label}
                        </button>
                    ))}
                </div>
                <div className="flex gap-2">
                    <input
                        value={input}
                        onChange={e => setInput(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder={connected
                            ? (lang === 'zh' ? '输入目标，如: 创建一个登录页面...' : 'Enter goal: Build a login page...')
                            : (lang === 'zh' ? '后端未连接...' : 'Backend not connected...')}
                        className="flex-1 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-[11px] text-[var(--text1)] placeholder:text-[var(--text3)] focus:outline-none focus:border-[var(--blue)] transition-colors"
                    />
                    <button onClick={handleSend} className="btn btn-primary text-[11px]">
                        🚀
                    </button>
                </div>
                <div className="text-[8px] text-[var(--text3)] mt-1.5 text-center">
                    {connected
                        ? `🟢 ${lang === 'zh' ? '自主模式 — AI 将自动计划、执行、测试' : 'Autonomous — AI will plan, execute, test'}`
                        : `🔴 ${lang === 'zh' ? '离线 — 启动后端: python server.py' : 'Offline — start backend: python server.py'}`}
                </div>
            </div>
        </div>
    );
}
