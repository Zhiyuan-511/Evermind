'use client';

import { useState, useCallback, useMemo } from 'react';
import { type ChatHistorySession } from '@/lib/types';

interface HistoryModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
    sessions: ChatHistorySession[];
    activeSessionId: string;
    onSelectSession: (sessionId: string) => void;
    onCreateSession: () => void;
    onDeleteSession: (sessionId: string) => void;
    onRenameSession?: (sessionId: string, newTitle: string) => void;
}

function formatTime(ts: number, lang: 'en' | 'zh'): string {
    try {
        return new Date(ts).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', {
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
        });
    } catch {
        return String(ts);
    }
}

function exportSessionAsMarkdown(session: ChatHistorySession): string {
    const lines: string[] = [`# ${session.title}`, `> Created: ${new Date(session.createdAt).toISOString()}`, ''];
    for (const msg of session.messages) {
        const role = msg.role === 'user' ? '**You**' : msg.role === 'agent' ? '**AI**' : `_${msg.sender || 'System'}_`;
        lines.push(`${role}: ${msg.content}`, '');
    }
    return lines.join('\n');
}

function downloadText(content: string, filename: string) {
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

export default function HistoryModal({
    open,
    onClose,
    lang,
    sessions,
    activeSessionId,
    onSelectSession,
    onCreateSession,
    onDeleteSession,
    onRenameSession,
}: HistoryModalProps) {
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editTitle, setEditTitle] = useState('');
    const [searchQuery, setSearchQuery] = useState('');

    const t = useCallback((en: string, zh: string) => (lang === 'zh' ? zh : en), [lang]);

    const filteredSessions = useMemo(() => {
        if (!searchQuery.trim()) return sessions;
        const q = searchQuery.toLowerCase();
        return sessions.filter(s =>
            s.title.toLowerCase().includes(q) ||
            s.messages.some(m => m.content.toLowerCase().includes(q))
        );
    }, [sessions, searchQuery]);

    const handleStartEdit = (e: React.MouseEvent, session: ChatHistorySession) => {
        e.stopPropagation();
        setEditingId(session.id);
        setEditTitle(session.title);
    };

    const handleSaveEdit = (e?: React.FormEvent | React.FocusEvent) => {
        e?.preventDefault();
        if (editingId && editTitle.trim() && onRenameSession) {
            onRenameSession(editingId, editTitle.trim());
        }
        setEditingId(null);
    };

    const handleExport = (e: React.MouseEvent, session: ChatHistorySession) => {
        e.stopPropagation();
        const md = exportSessionAsMarkdown(session);
        const safeName = session.title.replace(/[^a-zA-Z0-9\u4e00-\u9fa5_-]/g, '_').slice(0, 40);
        downloadText(md, `${safeName}.md`);
    };

    if (!open) return null;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-container" onClick={(e) => e.stopPropagation()} style={{ width: 860, maxWidth: '92vw' }}>
                <div className="modal-header">
                    <h3>🕘 {t('History', '历史会话')}</h3>
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button className="btn btn-primary text-[11px]" onClick={onCreateSession}>
                            ➕ {t('New Session', '新建会话')}
                        </button>
                        <button className="modal-close" onClick={onClose}>✕</button>
                    </div>
                </div>

                {/* Search */}
                <div style={{ padding: '8px 16px 0' }}>
                    <input
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        placeholder={t('Search sessions...', '搜索会话...')}
                        style={{
                            width: '100%',
                            padding: '6px 10px',
                            fontSize: 11,
                            borderRadius: 8,
                            border: '1px solid rgba(255,255,255,0.1)',
                            background: 'rgba(255,255,255,0.04)',
                            color: 'var(--text1)',
                            outline: 'none',
                        }}
                    />
                </div>

                <div className="modal-body" style={{ display: 'grid', gap: 10 }}>
                    {filteredSessions.length === 0 ? (
                        <div className="text-[12px] text-[var(--text3)]">
                            {searchQuery ? t('No matching sessions', '没有匹配的会话') : t('No history yet', '暂无历史记录')}
                        </div>
                    ) : (
                        filteredSessions.map((s) => {
                            const userMsg = s.messages.slice().reverse().find((m) => m.role === 'user');
                            const anyMsg = !userMsg ? s.messages.slice().reverse().find((m) => m.content.trim().length > 5 && !m.content.startsWith('<b>')) : null;
                            const preview = (userMsg?.content || anyMsg?.content?.replace(/<[^>]+>/g, '') || '').trim();
                            const active = s.id === activeSessionId;
                            const isEditing = editingId === s.id;
                            return (
                                <div
                                    key={s.id}
                                    className="glass"
                                    onClick={() => onSelectSession(s.id)}
                                    style={{
                                        padding: 12,
                                        borderRadius: 10,
                                        cursor: 'pointer',
                                        borderColor: active ? 'var(--blue)' : 'var(--glass-border)',
                                        boxShadow: active ? '0 0 0 1px color-mix(in srgb, var(--blue) 75%, transparent)' : undefined,
                                    }}
                                >
                                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
                                        {isEditing ? (
                                            <form onSubmit={handleSaveEdit} onClick={(e) => e.stopPropagation()} style={{ flex: 1 }}>
                                                <input
                                                    value={editTitle}
                                                    onChange={(e) => setEditTitle(e.target.value)}
                                                    onBlur={handleSaveEdit}
                                                    autoFocus
                                                    maxLength={80}
                                                    style={{
                                                        width: '100%',
                                                        fontSize: 12,
                                                        fontWeight: 700,
                                                        color: 'var(--text1)',
                                                        background: 'rgba(255,255,255,0.06)',
                                                        border: '1px solid var(--blue)',
                                                        borderRadius: 4,
                                                        padding: '2px 6px',
                                                        outline: 'none',
                                                    }}
                                                />
                                            </form>
                                        ) : (
                                            <div
                                                style={{
                                                    fontSize: 12, fontWeight: 700, color: 'var(--text1)', cursor: 'text',
                                                    overflow: 'hidden', textOverflow: 'ellipsis',
                                                    display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' as const,
                                                    wordBreak: 'break-word' as const, lineHeight: 1.4,
                                                }}
                                                onDoubleClick={(e) => handleStartEdit(e, s)}
                                                title={t('Double-click to rename', '双击重命名')}
                                            >
                                                {s.title || t('Untitled Session', '未命名会话')}
                                            </div>
                                        )}
                                        <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                                            <button
                                                className="btn text-[10px]"
                                                onClick={(e) => handleExport(e, s)}
                                                title={t('Export as Markdown', '导出为 Markdown')}
                                            >
                                                📤
                                            </button>
                                            <button
                                                className="btn text-[10px]"
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    onDeleteSession(s.id);
                                                }}
                                            >
                                                🗑
                                            </button>
                                        </div>
                                    </div>
                                    <div style={{ fontSize: 10, color: 'var(--text3)', marginTop: 4 }}>
                                        {formatTime(s.updatedAt, lang)} · {s.messages.length} {t('messages', '条消息')}
                                    </div>
                                    <div style={{
                                        marginTop: 6,
                                        fontSize: 11,
                                        color: 'var(--text2)',
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        display: '-webkit-box',
                                        WebkitLineClamp: 2,
                                        WebkitBoxOrient: 'vertical' as const,
                                        wordBreak: 'break-word' as const,
                                        lineHeight: 1.45,
                                    }}>
                                        {preview || t('No user prompt yet', '还没有用户输入')}
                                    </div>
                                </div>
                            );
                        })
                    )}
                </div>
            </div>
        </div>
    );
}
