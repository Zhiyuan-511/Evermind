'use client';

import { useCallback, useEffect, useState } from 'react';
import type { ChatMessage, ChatHistorySession } from '@/lib/types';

// ── Constants ──
const CHAT_HISTORY_STORAGE_KEY = 'evermind-chat-history-v1';
const ACTIVE_CHAT_SESSION_STORAGE_KEY = 'evermind-active-chat-session-v1';
const MAX_HISTORY_SESSIONS = 30;
const MAX_MESSAGES_PER_SESSION = 400;

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
    if (!firstUser) return existingTitle || defaultSessionTitle(lang);
    return firstUser.content.replace(/\s+/g, ' ').trim().slice(0, 42);
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
    addMessage: (role: 'user' | 'system' | 'agent', content: string, sender?: string, icon?: string, borderColor?: string) => void;
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
            const active = sessions.find((s) => s.id === savedActiveId) || sessions[0];
            setHistorySessions(sessions);
            setActiveSessionId(active.id);
            setMessages(active.messages);
            if (active.title) setWorkflowName(active.title);
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
    ) => {
        const msg: ChatMessage = {
            id: Date.now().toString(36) + Math.random().toString(36).slice(2),
            role,
            content: content.slice(0, 12000),
            sender, icon, borderColor,
            timestamp: now(),
        };
        setMessages((prev) => [...prev, msg].slice(-MAX_MESSAGES_PER_SESSION));
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
