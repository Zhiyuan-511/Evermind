'use client';

import { useCallback, useEffect, useState } from 'react';
import type { ChatAttachment, ChatMessage, ChatHistorySession } from '@/lib/types';
import { normalizeChatAttachment } from '@/lib/chatAttachments';

// ── Constants ──
const CHAT_HISTORY_STORAGE_KEY = 'evermind-chat-history-v1';
const ACTIVE_CHAT_SESSION_STORAGE_KEY = 'evermind-active-chat-session-v1';
const MAX_HISTORY_SESSIONS = 30;
const MAX_MESSAGES_PER_SESSION = 800;
const MAX_VISIBLE_MESSAGES = 300;
const MAX_CONSOLE_MESSAGES = 500;

// ── Helpers ──
function now() {
    return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function createSessionId(): string {
    return `session_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function defaultSessionTitle(lang: 'en' | 'zh'): string {
    const time = new Date().toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-US', {
        hour: '2-digit', minute: '2-digit', hour12: false,
    });
    return lang === 'zh' ? `会话 ${time}` : `Session ${time}`;
}

function isDefaultSessionTitle(title: string): boolean {
    return title.startsWith('会话 ') || title.startsWith('Session ');
}

function inferSessionTitle(messages: ChatMessage[], lang: 'en' | 'zh', existingTitle: string): string {
    if (existingTitle && !isDefaultSessionTitle(existingTitle)) return existingTitle;
    const firstUser = messages.find((m) => m.role === 'user' && m.content.trim());
    if (firstUser) return firstUser.content.replace(/\s+/g, ' ').trim().slice(0, 42);
    // Fallback: use first meaningful agent/system message (skip generic headers)
    const skipPrefixes = ['预览已就绪', 'Preview ready', '后端已连接', 'Connected'];
    const firstMeaningful = messages.find((m) =>
        m.content.trim().length > 5
        && !skipPrefixes.some(p => m.content.startsWith(p))
        && !m.content.startsWith('<b>')
    );
    if (firstMeaningful) return firstMeaningful.content.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim().slice(0, 42);
    return existingTitle || defaultSessionTitle(lang);
}

function normalizeCompletionData(value: unknown): ChatMessage['completionData'] | undefined {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
    const record = value as Record<string, unknown>;
    const subtasks = Array.isArray(record.subtasks)
        ? record.subtasks.reduce<NonNullable<ChatMessage['completionData']>['subtasks']>((acc, item) => {
            if (!item || typeof item !== 'object' || Array.isArray(item)) return acc;
            const subtask = item as Record<string, unknown>;
            const id = String(subtask.id || '').trim();
            const agent = String(subtask.agent || '').trim();
            if (!id || !agent) return acc;
            acc.push({
                id,
                agent,
                status: String(subtask.status || 'unknown').trim() || 'unknown',
                retries: Math.max(0, Number(subtask.retries || 0)),
                filesCreated: Array.isArray(subtask.filesCreated)
                    ? subtask.filesCreated.filter((entry): entry is string => typeof entry === 'string' && entry.trim().length > 0)
                    : undefined,
                workSummary: Array.isArray(subtask.workSummary)
                    ? subtask.workSummary.filter((entry): entry is string => typeof entry === 'string' && entry.trim().length > 0)
                    : undefined,
            });
            return acc;
        }, [])
        : [];

    return {
        success: Boolean(record.success),
        completed: Math.max(0, Number(record.completed || 0)),
        total: Math.max(0, Number(record.total || 0)),
        retries: Math.max(0, Number(record.retries || 0)),
        durationSeconds: Math.max(0, Number(record.durationSeconds || 0)),
        difficulty: String(record.difficulty || 'standard'),
        subtasks,
        previewUrl: typeof record.previewUrl === 'string' ? record.previewUrl : undefined,
    };
}

function normalizeAttachments(value: unknown): ChatAttachment[] | undefined {
    if (!Array.isArray(value)) return undefined;
    const attachments = value
        .map((item) => normalizeChatAttachment(item))
        .filter((item): item is ChatAttachment => !!item);
    return attachments.length > 0 ? attachments : undefined;
}

function normalizeMessage(msg: unknown): ChatMessage | null {
    if (!msg || typeof msg !== 'object') return null;
    const value = msg as Partial<ChatMessage>;
    if (!value.content || typeof value.content !== 'string') return null;
    if (!value.role || !['user', 'system', 'agent'].includes(value.role)) return null;
    return {
        id: typeof value.id === 'string' ? value.id : Date.now().toString(36),
        role: value.role,
        content: value.content.slice(0, 12000),
        sender: typeof value.sender === 'string' ? value.sender : undefined,
        icon: typeof value.icon === 'string' ? value.icon : undefined,
        timestamp: typeof value.timestamp === 'string' ? value.timestamp : now(),
        borderColor: typeof value.borderColor === 'string' ? value.borderColor : undefined,
        attachments: normalizeAttachments((value as Record<string, unknown>).attachments),
        completionData: normalizeCompletionData(value.completionData),
    };
}

function normalizeSession(raw: unknown, lang: 'en' | 'zh'): ChatHistorySession | null {
    if (!raw || typeof raw !== 'object') return null;
    const value = raw as Partial<ChatHistorySession>;
    const rawMessages = Array.isArray(value.messages) ? value.messages : [];
    const messages = rawMessages
        .map((m) => normalizeMessage(m))
        .filter((m): m is ChatMessage => !!m)
        .slice(-MAX_MESSAGES_PER_SESSION);
    const createdAt = typeof value.createdAt === 'number' ? value.createdAt : Date.now();
    const updatedAt = typeof value.updatedAt === 'number' ? value.updatedAt : createdAt;
    const title = typeof value.title === 'string' && value.title.trim()
        ? value.title.trim().slice(0, 80)
        : inferSessionTitle(messages, lang, defaultSessionTitle(lang));
    return {
        id: typeof value.id === 'string' ? value.id : createSessionId(),
        title, createdAt, updatedAt, messages,
    };
}

function createSession(lang: 'en' | 'zh', initialMessages: ChatMessage[] = []): ChatHistorySession {
    const ts = Date.now();
    const messages = initialMessages.slice(-MAX_MESSAGES_PER_SESSION);
    return {
        id: createSessionId(),
        title: inferSessionTitle(messages, lang, defaultSessionTitle(lang)),
        createdAt: ts, updatedAt: ts, messages,
    };
}

// ── Hook ──
export interface UseChatHistoryReturn {
    messages: ChatMessage[];
    setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
    historySessions: ChatHistorySession[];
    activeSessionId: string;
    workflowName: string;
    addMessage: (role: 'user' | 'system' | 'agent', content: string, sender?: string, icon?: string, borderColor?: string, completionData?: ChatMessage['completionData'], attachments?: ChatAttachment[]) => void;
    handleCreateSession: () => void;
    handleSelectSession: (sessionId: string) => void;
    handleDeleteSession: (sessionId: string) => void;
    handleRenameSession: (sessionId: string, title: string) => void;
    handleWorkflowNameChange: (name: string) => void;
    resetForPreview: () => void;
}

export function useChatHistory(lang: 'en' | 'zh'): UseChatHistoryReturn {
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [historySessions, setHistorySessions] = useState<ChatHistorySession[]>([]);
    const [activeSessionId, setActiveSessionId] = useState('');
    const [workflowName, setWorkflowName] = useState('Workflow 1');

    // ── Load sessions from localStorage on mount ──
    useEffect(() => {
        if (typeof window === 'undefined') return;
        try {
            const raw = window.localStorage.getItem(CHAT_HISTORY_STORAGE_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            const normalized = (Array.isArray(parsed) ? parsed : [])
                .map((item) => normalizeSession(item, lang))
                .filter((item): item is ChatHistorySession => !!item)
                .sort((a, b) => b.updatedAt - a.updatedAt)
                .slice(0, MAX_HISTORY_SESSIONS);
            const sessions = normalized.length > 0 ? normalized : [createSession(lang)];
            const savedActiveId = window.localStorage.getItem(ACTIVE_CHAT_SESSION_STORAGE_KEY) || '';
            const preferredActive = sessions.find((s) => s.id === savedActiveId) || sessions[0];
            const reusableEmpty = sessions.find((s) => s.messages.length === 0) || null;

            // §3.3: Reuse an existing empty session or create a fresh one on first app open in this tab.
            const freshFlag = window.sessionStorage.getItem('evermind-fresh-session');
            if (!freshFlag && reusableEmpty) {
                setHistorySessions(sessions);
                setActiveSessionId(reusableEmpty.id);
                setMessages([]);
                setWorkflowName(reusableEmpty.title);
                window.sessionStorage.setItem('evermind-fresh-session', '1');
            } else if (!freshFlag && preferredActive?.messages.length > 0) {
                // §FIX: Previously created a new empty session here, losing all chat history
                // during page reloads. Now load the active session's messages directly.
                setHistorySessions(sessions);
                setActiveSessionId(preferredActive.id);
                setMessages(preferredActive.messages);
                if (preferredActive.title) setWorkflowName(preferredActive.title);
                window.sessionStorage.setItem('evermind-fresh-session', '1');
            } else {
                const active = reusableEmpty && !freshFlag ? reusableEmpty : preferredActive;
                setHistorySessions(sessions);
                setActiveSessionId(active.id);
                setMessages(active.messages);
                if (active.title) setWorkflowName(active.title);
                if (!freshFlag) window.sessionStorage.setItem('evermind-fresh-session', '1');
            }
        } catch {
            const fallback = createSession(lang);
            setHistorySessions([fallback]);
            setActiveSessionId(fallback.id);
            setMessages([]);
            setWorkflowName(fallback.title);
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // ── Persist sessions to localStorage ──
    useEffect(() => {
        if (typeof window === 'undefined' || historySessions.length === 0) return;
        try {
            window.localStorage.setItem(CHAT_HISTORY_STORAGE_KEY, JSON.stringify(historySessions));
        } catch { /* ignore */ }
    }, [historySessions]);

    // ── Persist active session ID ──
    useEffect(() => {
        if (typeof window === 'undefined' || !activeSessionId) return;
        try {
            window.localStorage.setItem(ACTIVE_CHAT_SESSION_STORAGE_KEY, activeSessionId);
        } catch { /* ignore */ }
    }, [activeSessionId]);

    // ── Sync messages to active session ──
    useEffect(() => {
        if (!activeSessionId) return;
        setHistorySessions((prev) => {
            let changed = false;
            const nowTs = Date.now();
            const normalizedMessages = messages.slice(-MAX_MESSAGES_PER_SESSION);
            const updated = prev.map((session) => {
                if (session.id !== activeSessionId) return session;
                const sameMessages =
                    session.messages.length === normalizedMessages.length
                    && session.messages.every((m, i) => (
                        m.id === normalizedMessages[i]?.id
                        && m.role === normalizedMessages[i]?.role
                        && m.content === normalizedMessages[i]?.content
                        && m.timestamp === normalizedMessages[i]?.timestamp
                        && JSON.stringify(m.attachments || null) === JSON.stringify(normalizedMessages[i]?.attachments || null)
                        && JSON.stringify(m.completionData || null) === JSON.stringify(normalizedMessages[i]?.completionData || null)
                    ));
                const nextTitle = inferSessionTitle(normalizedMessages, lang, session.title);
                if (sameMessages && nextTitle === session.title) return session;
                changed = true;
                return { ...session, title: nextTitle, updatedAt: nowTs, messages: normalizedMessages };
            });
            if (!changed) return prev;
            return updated.sort((a, b) => b.updatedAt - a.updatedAt).slice(0, MAX_HISTORY_SESSIONS);
        });
    }, [activeSessionId, lang, messages]);

    // ── Add message ──
    const addMessage = useCallback((
        role: 'user' | 'system' | 'agent',
        content: string,
        sender?: string,
        icon?: string,
        borderColor?: string,
        completionData?: ChatMessage['completionData'],
        attachments?: ChatAttachment[],
    ) => {
        const msg: ChatMessage = {
            id: Date.now().toString(36) + Math.random().toString(36).slice(2),
            role,
            content: content.slice(0, 12000),
            sender, icon, borderColor,
            timestamp: now(),
            completionData,
            attachments: attachments && attachments.length > 0 ? attachments : undefined,
        };
        // §FIX: Apply separate caps for console logs vs visible messages
        // to prevent heartbeat log flooding from evicting milestone messages.
        setMessages((prev) => {
            const next = [...prev, msg];
            if (next.length <= MAX_MESSAGES_PER_SESSION) return next;
            // Split into visible (milestone/user) and console messages
            const visible: ChatMessage[] = [];
            const console: ChatMessage[] = [];
            for (const m of next) {
                if (m.sender === 'console') {
                    console.push(m);
                } else {
                    visible.push(m);
                }
            }
            // Apply separate caps, keeping newest of each category
            const cappedVisible = visible.length > MAX_VISIBLE_MESSAGES
                ? visible.slice(-MAX_VISIBLE_MESSAGES)
                : visible;
            const cappedConsole = console.length > MAX_CONSOLE_MESSAGES
                ? console.slice(-MAX_CONSOLE_MESSAGES)
                : console;
            // Merge back in chronological order by id (timestamp-based)
            return [...cappedVisible, ...cappedConsole]
                .sort((a, b) => a.id.localeCompare(b.id));
        });
    }, []);

    // ── Session CRUD ──
    const handleCreateSession = useCallback(() => {
        const newSession = createSession(lang);
        setHistorySessions((prev) => [newSession, ...prev].slice(0, MAX_HISTORY_SESSIONS));
        setActiveSessionId(newSession.id);
        setMessages([]);
        setWorkflowName(newSession.title);
    }, [lang]);

    const handleSelectSession = useCallback((sessionId: string) => {
        const session = historySessions.find((s) => s.id === sessionId);
        if (!session) return;
        setActiveSessionId(session.id);
        setMessages(session.messages || []);
        setWorkflowName(session.title || workflowName);
        setHistorySessions((prev) => prev.map((item) => (
            item.id === session.id ? { ...item, updatedAt: Date.now() } : item
        )).sort((a, b) => b.updatedAt - a.updatedAt));
    }, [historySessions, workflowName]);

    const handleDeleteSession = useCallback((sessionId: string) => {
        let nextActiveId = activeSessionId;
        let nextMessages: ChatMessage[] | null = null;
        let nextWorkflowName: string | null = null;
        setHistorySessions((prev) => {
            const remaining = prev.filter((s) => s.id !== sessionId);
            if (remaining.length === 0) {
                const fresh = createSession(lang);
                nextActiveId = fresh.id;
                nextMessages = [];
                nextWorkflowName = fresh.title;
                return [fresh];
            }
            if (sessionId === activeSessionId) {
                const replacement = remaining[0];
                nextActiveId = replacement.id;
                nextMessages = replacement.messages || [];
                nextWorkflowName = replacement.title;
            }
            return remaining;
        });
        if (nextActiveId !== activeSessionId) setActiveSessionId(nextActiveId);
        if (nextMessages) setMessages(nextMessages);
        if (nextWorkflowName) setWorkflowName(nextWorkflowName);
    }, [activeSessionId, lang]);

    const handleRenameSession = useCallback((sessionId: string, newTitle: string) => {
        setHistorySessions((prev) => prev.map((s) =>
            s.id === sessionId ? { ...s, title: newTitle.slice(0, 80), updatedAt: Date.now() } : s
        ));
        if (sessionId === activeSessionId) setWorkflowName(newTitle.slice(0, 80));
    }, [activeSessionId]);

    const handleWorkflowNameChange = useCallback((name: string) => {
        const nextName = name.slice(0, 80);
        setWorkflowName(nextName);
        if (!activeSessionId) return;
        setHistorySessions((prev) => prev.map((session) => {
            if (session.id !== activeSessionId) return session;
            const fallback = defaultSessionTitle(lang);
            return { ...session, title: nextName.trim() ? nextName : fallback, updatedAt: Date.now() };
        }).sort((a, b) => b.updatedAt - a.updatedAt));
    }, [activeSessionId, lang]);

    const resetForPreview = useCallback(() => {
        // Called when switching sessions — allows parent to reset preview state
    }, []);

    return {
        messages, setMessages, historySessions, activeSessionId, workflowName,
        addMessage, handleCreateSession, handleSelectSession, handleDeleteSession,
        handleRenameSession, handleWorkflowNameChange, resetForPreview,
    };
}
