'use client';

import { useState, useRef, useEffect, useCallback } from 'react';

interface FileDiff {
    path: string;
    original_content?: string;
    new_content?: string;
}

interface DirectChatPanelProps {
    wsRef: React.RefObject<WebSocket | null>;
    connected: boolean;
    lang: 'en' | 'zh';
    sessionId?: string;
    onOpenFile?: (path: string, root: string, content: string, ext: string) => void;
    onFileDiffs?: (diffs: FileDiff[]) => void;
}

interface DirectChatMessage {
    role: 'user' | 'assistant' | 'system';
    content: string;
    ts: number;
    model?: string;
    streaming?: boolean;
    filesModified?: string[];
    fileDiffs?: FileDiff[];
}

const MODEL_OPTIONS = [
    { id: '', label: 'Auto' },
    { id: 'gpt-5.4', label: 'GPT-5.4' },
    { id: 'gpt-5.4-mini', label: 'GPT-5.4 Mini' },
    { id: 'claude-4-sonnet', label: 'Claude 4 Sonnet' },
    { id: 'kimi-coding', label: 'Kimi Coding' },
    { id: 'deepseek-v3', label: 'DeepSeek V3' },
    { id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
];

export default function DirectChatPanel({ wsRef, connected, lang, sessionId, onOpenFile, onFileDiffs }: DirectChatPanelProps) {
    const [messages, setMessages] = useState<DirectChatMessage[]>([]);
    const [input, setInput] = useState('');
    const [model, setModel] = useState('');
    const [streaming, setStreaming] = useState(false);
    const [convId] = useState(() => `conv_${Date.now()}`);
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const streamBufferRef = useRef('');
    const listenerAttachedRef = useRef(false);

    const scrollToBottom = useCallback(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, []);

    useEffect(() => { scrollToBottom(); }, [messages, scrollToBottom]);

    // Listen for WS messages related to direct chat
    useEffect(() => {
        const ws = wsRef.current;
        if (!ws || listenerAttachedRef.current) return;
        listenerAttachedRef.current = true;

        const handler = (event: MessageEvent) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'chat_token' && data.conversation_id === convId) {
                    streamBufferRef.current += data.token || '';
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.streaming) {
                            return [...prev.slice(0, -1), { ...last, content: streamBufferRef.current }];
                        }
                        return prev;
                    });
                } else if (data.type === 'chat_complete' && data.conversation_id === convId) {
                    const filesModified: string[] = Array.isArray(data.files_modified) ? data.files_modified : [];
                    const fileDiffs: FileDiff[] = Array.isArray(data.file_diffs) ? data.file_diffs : [];
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.streaming) {
                            return [...prev.slice(0, -1), {
                                ...last,
                                content: data.content || streamBufferRef.current,
                                streaming: false,
                                filesModified: filesModified.length > 0 ? filesModified : undefined,
                                fileDiffs: fileDiffs.length > 0 ? fileDiffs : undefined,
                            }];
                        }
                        return prev;
                    });
                    streamBufferRef.current = '';
                    setStreaming(false);
                    // Notify parent about file diffs for CodeEditorPanel
                    if (fileDiffs.length > 0 && onFileDiffs) {
                        onFileDiffs(fileDiffs);
                    }
                } else if (data.type === 'chat_error' && data.conversation_id === convId) {
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.streaming) {
                            return [...prev.slice(0, -1), { ...last, content: `Error: ${data.error || 'Unknown error'}`, streaming: false }];
                        }
                        return [...prev, { role: 'system', content: `Error: ${data.error}`, ts: Date.now() }];
                    });
                    streamBufferRef.current = '';
                    setStreaming(false);
                } else if (data.type === 'chat_ack' && data.conversation_id === convId) {
                    // ACK received — streaming will follow
                }
            } catch { /* ignore non-JSON messages */ }
        };

        ws.addEventListener('message', handler);
        return () => {
            ws.removeEventListener('message', handler);
            listenerAttachedRef.current = false;
        };
    }, [wsRef, convId]);

    const sendMessage = useCallback(() => {
        const text = input.trim();
        if (!text || streaming || !connected) return;

        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;

        // Add user message
        setMessages(prev => [...prev, { role: 'user', content: text, ts: Date.now() }]);

        // Add streaming placeholder
        streamBufferRef.current = '';
        setMessages(prev => [...prev, {
            role: 'assistant', content: '', ts: Date.now(),
            model: model || 'Auto', streaming: true,
        }]);
        setStreaming(true);
        setInput('');

        // Build history for context
        const history = messages.filter(m => m.role === 'user' || m.role === 'assistant')
            .map(m => ({ role: m.role, content: m.content }))
            .slice(-50);

        ws.send(JSON.stringify({
            type: 'chat_message',
            message: text,
            model: model,
            conversation_id: convId,
            session_id: sessionId || '',
            history,
        }));
    }, [input, streaming, connected, wsRef, model, convId, sessionId, messages]);

    const handleClear = useCallback(() => {
        setMessages([]);
        streamBufferRef.current = '';
        setStreaming(false);
    }, []);

    const handleStop = useCallback(() => {
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'chat_stop' }));
        }
        setStreaming(false);
    }, [wsRef]);

    const t = lang === 'zh' ? {
        placeholder: '直接与 AI 对话...',
        send: '发送',
        stop: '停止',
        clear: '清空',
        welcome: '💬 Chat 模式 — 直接与 AI 对话，适合快速提问、debug 和小幅修改。',
        notConnected: '未连接到后端',
    } : {
        placeholder: 'Chat with AI...',
        send: 'Send',
        stop: 'Stop',
        clear: 'Clear',
        welcome: '💬 Chat mode — talk directly with AI for quick questions, debugging, and small edits.',
        notConnected: 'Not connected to backend',
    };

    return (
        <div className="flex flex-col h-full" style={{ background: 'var(--canvas-bg, #0d0d1a)' }}>
            {/* Header bar */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', borderBottom: '1px solid rgba(255,255,255,0.06)', flexShrink: 0 }}>
                <select
                    value={model}
                    onChange={e => setModel(e.target.value)}
                    style={{
                        flex: 1, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)',
                        borderRadius: 6, fontSize: 11, color: 'var(--text2, #aaa)', padding: '4px 8px', outline: 'none',
                    }}
                >
                    {MODEL_OPTIONS.map(m => (
                        <option key={m.id} value={m.id}>{m.label}</option>
                    ))}
                </select>
                <button onClick={handleClear} style={{ fontSize: 12, color: 'var(--text3)', cursor: 'pointer', background: 'none', border: 'none', padding: '2px 4px' }} title={t.clear}>
                    🗑
                </button>
            </div>

            {/* Messages area */}
            <div className="flex-1 overflow-y-auto" style={{ padding: '12px 10px', minHeight: 0 }}>
                {messages.length === 0 && (
                    <div style={{ textAlign: 'center', padding: '40px 16px', color: 'var(--text3, #666)', fontSize: 11, lineHeight: 1.6 }}>
                        <div style={{ fontSize: 24, opacity: 0.3, marginBottom: 8 }}>💬</div>
                        {t.welcome}
                    </div>
                )}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {messages.map((msg, i) => (
                    <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: msg.role === 'user' ? 'flex-end' : 'flex-start' }}>
                        <div style={{
                            maxWidth: '88%', borderRadius: 10, padding: '8px 12px', fontSize: 12, lineHeight: 1.5,
                            ...(msg.role === 'user' ? {
                                background: 'rgba(79,143,255,0.15)', border: '1px solid rgba(79,143,255,0.2)', color: '#c4d9ff',
                            } : msg.role === 'system' ? {
                                background: 'rgba(248,81,73,0.12)', border: '1px solid rgba(248,81,73,0.2)', color: '#ffa8a8',
                            } : {
                                background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)', color: 'var(--text1, #eee)',
                            }),
                        }}>
                            {msg.role === 'assistant' && msg.model && (
                                <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 4, fontWeight: 500 }}>{msg.model}</div>
                            )}
                            <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                {msg.content || (msg.streaming ? '...' : '')}
                                {msg.streaming && <span className="animate-pulse" style={{ marginLeft: 2 }}>▊</span>}
                            </div>
                            {msg.filesModified && msg.filesModified.length > 0 && (
                                <div style={{ marginTop: 8, border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6, padding: '6px 8px', background: 'rgba(255,255,255,0.02)' }}>
                                    <div style={{ fontSize: 10, color: '#a855f7', marginBottom: 4, fontWeight: 600 }}>
                                        Files Modified ({msg.filesModified.length})
                                    </div>
                                    {msg.filesModified.map((f, fi) => (
                                        <button
                                            key={fi}
                                            onClick={() => {
                                                const ext = f.split('.').pop() || '';
                                                const diff = msg.fileDiffs?.find(d => d.path === f);
                                                onOpenFile?.(f, '', diff?.new_content || '', ext);
                                            }}
                                            style={{
                                                display: 'block', width: '100%', textAlign: 'left', fontSize: 10,
                                                color: '#60a5fa', padding: '2px 6px', borderRadius: 4, cursor: 'pointer',
                                                background: 'none', border: 'none', fontFamily: 'var(--font-mono, monospace)',
                                            }}
                                            onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.05)')}
                                            onMouseLeave={e => (e.currentTarget.style.background = 'none')}
                                        >
                                            {f.split('/').pop()}
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>
                    </div>
                ))}
                </div>
                <div ref={messagesEndRef} />
            </div>

            {/* Input area — pinned at bottom */}
            <div style={{ padding: '8px 10px', borderTop: '1px solid rgba(255,255,255,0.06)', flexShrink: 0 }}>
                <div style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)',
                    borderRadius: 10, padding: '6px 10px',
                }}>
                    <input
                        value={input}
                        onChange={e => setInput(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
                        placeholder={connected ? t.placeholder : t.notConnected}
                        disabled={!connected}
                        style={{
                            flex: 1, background: 'transparent', border: 'none', outline: 'none',
                            fontSize: 12, color: 'var(--text1, #eee)', minWidth: 0,
                        }}
                    />
                    {streaming ? (
                        <button onClick={handleStop} style={{
                            background: 'rgba(239,68,68,0.8)', color: '#fff', fontSize: 11, fontWeight: 600,
                            padding: '4px 12px', borderRadius: 6, border: 'none', cursor: 'pointer', flexShrink: 0,
                        }}>
                            {t.stop}
                        </button>
                    ) : (
                        <button onClick={sendMessage} disabled={!connected || !input.trim()} style={{
                            background: !connected || !input.trim() ? 'rgba(79,143,255,0.3)' : 'rgba(79,143,255,0.8)',
                            color: '#fff', fontSize: 11, fontWeight: 600,
                            padding: '4px 12px', borderRadius: 6, border: 'none',
                            cursor: !connected || !input.trim() ? 'default' : 'pointer', flexShrink: 0,
                            opacity: !connected || !input.trim() ? 0.5 : 1,
                        }}>
                            {t.send}
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
