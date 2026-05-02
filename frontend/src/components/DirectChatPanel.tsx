'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import ReasoningBlock from './ReasoningBlock';
import ToolCallBlock, { type ToolCallTrace } from './ToolCallBlock';

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
    // v6.4.29 (maintainer) — collapsible thinking bubble.
    // `reasoning` accumulates reasoning_content stream deltas; `reasoningActive`
    // toggles between "thinking live" and "done — collapsed". When the first
    // main content token arrives, reasoningActive goes false and the bubble
    // auto-collapses (user can re-expand via click).
    reasoning?: string;
    reasoningActive?: boolean;
    reasoningStartedAt?: number;
    reasoningEndedAt?: number;
    // v6.4.32: foldable tool-call trace. Each entry is keyed by id and
    // updated in-place when the result arrives.
    toolCalls?: ToolCallTrace[];
}

// v5.8: model options are derived from the user's actual settings at runtime
// instead of being hardcoded. A hardcoded list showed 7 models even when the
// user only had one provider's key configured, leading to "pick a model
// that 403s" UX. We now pull unique model ids from node_model_preferences
// + default_model via /api/settings and fall back to the active default only.
const FALLBACK_MODEL_OPTIONS = [
    { id: '', label: 'Auto' },
    { id: 'kimi-k2.6-code-preview', label: 'Kimi K2.6 Code (Preview)' },
];

type ModelOption = { id: string; label: string };

// Humanize a model id for the dropdown label (strip provider prefix, title-case).
function labelForModel(id: string): string {
    if (!id) return 'Auto';
    const clean = id.replace(/^openai\//, '').replace(/-/g, ' ');
    return clean.replace(/\b\w/g, (c) => c.toUpperCase());
}

const CHAT_STORAGE_KEY = 'evermind-direct-chat-v1';
const MAX_PERSISTED_MESSAGES = 200;

function loadPersistedMessages(sid: string): DirectChatMessage[] {
    if (!sid) return [];
    try {
        const raw = localStorage.getItem(CHAT_STORAGE_KEY);
        if (!raw) return [];
        const store = JSON.parse(raw);
        const msgs: DirectChatMessage[] = store[sid] || [];
        // Strip any leftover streaming state
        return msgs.map(m => ({ ...m, streaming: false }));
    } catch { return []; }
}

function persistMessages(sid: string, msgs: DirectChatMessage[]) {
    if (!sid) return;
    try {
        const raw = localStorage.getItem(CHAT_STORAGE_KEY);
        const store = raw ? JSON.parse(raw) : {};
        // Only persist non-streaming, non-empty messages
        store[sid] = msgs
            .filter(m => !m.streaming && m.content)
            .slice(-MAX_PERSISTED_MESSAGES);
        // Evict oldest sessions if store grows too large (keep 20 sessions max)
        const keys = Object.keys(store);
        if (keys.length > 20) {
            for (const k of keys.slice(0, keys.length - 20)) delete store[k];
        }
        localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(store));
    } catch { /* quota exceeded — silently skip */ }
}

export default function DirectChatPanel({ wsRef, connected, lang, sessionId, onOpenFile, onFileDiffs }: DirectChatPanelProps) {
    const [messages, setMessages] = useState<DirectChatMessage[]>(() => loadPersistedMessages(sessionId || ''));
    const [input, setInput] = useState('');
    const [model, setModel] = useState('');
    const [modelOptions, setModelOptions] = useState<ModelOption[]>(FALLBACK_MODEL_OPTIONS);
    const [streaming, setStreaming] = useState(false);

    // v5.8: derive the dropdown from live /api/settings. Only show models that
    // are actually in the user's node_model_preferences (plus the default),
    // so "pick a model that 403s" is no longer possible.
    // v5.8.6: pull available models from /api/models (has_key filter) so the
    // chat agent dropdown reflects real availability, not just what the user
    // happened to put in per-node preferences. This matches AgentNode and
    // SettingsModal behavior: every model shown has a configured key.
    useEffect(() => {
        let cancelled = false;
        const refresh = async () => {
            try {
                // v5.8.6 FIX: absolute URL — Electron renderer's base is the
                // Next.js dev server (localhost:3xxx), NOT the backend. A
                // relative '/api/models' 404'd, making the dropdown silently
                // fall back to the 2-item FALLBACK list.
                const base = (typeof window !== 'undefined' && (window as unknown as { __evermindApiBase?: string }).__evermindApiBase)
                    || 'http://127.0.0.1:8765';
                const r = await fetch(`${base}/api/models`, { cache: 'no-store' });
                if (!r.ok) return;
                const d = await r.json();
                if (cancelled) return;
                const models = Array.isArray(d?.models) ? d.models : [];
                const ids: string[] = models
                    .filter((m: { has_key?: boolean }) => m.has_key === true)
                    .map((m: { id: string }) => m.id);
                const opts: ModelOption[] = [{ id: '', label: 'Auto' }];
                ids.sort().forEach((id) => opts.push({ id, label: labelForModel(id) }));
                if (opts.length > 1) setModelOptions(opts);
            } catch {
                // fetch failed; stick with fallback defaults
            }
        };
        refresh();
        // v5.8.6: listen for settings-save broadcasts → live-refresh dropdown
        window.addEventListener('evermind-models-changed', refresh);
        return () => {
            cancelled = true;
            window.removeEventListener('evermind-models-changed', refresh);
        };
    }, []);
    // Stable conversation ID per session — survives remount
    const [convId] = useState(() => sessionId ? `conv_${sessionId}` : `conv_${Date.now()}`);
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const streamBufferRef = useRef('');
    const listenerAttachedRef = useRef(false);

    const scrollToBottom = useCallback(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, []);

    useEffect(() => { scrollToBottom(); }, [messages, scrollToBottom]);

    // Persist messages to localStorage whenever they change (skip during streaming)
    useEffect(() => {
        if (!streaming && sessionId) {
            persistMessages(sessionId, messages);
        }
    }, [messages, streaming, sessionId]);

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
                } else if (data.type === 'chat_reasoning_delta' && data.conversation_id === convId) {
                    // v6.4.29: accumulate reasoning into the streaming assistant
                    // message. Bubble stays visible + live until reasoning_done.
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.streaming) {
                            return [...prev.slice(0, -1), {
                                ...last,
                                reasoning: (last.reasoning || '') + (data.text || ''),
                                reasoningActive: true,
                                reasoningStartedAt: last.reasoningStartedAt || Date.now(),
                            }];
                        }
                        return prev;
                    });
                } else if (data.type === 'chat_reasoning_done' && data.conversation_id === convId) {
                    // Main content is about to start → stop the spinner,
                    // auto-collapse the reasoning bubble.
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.streaming) {
                            return [...prev.slice(0, -1), {
                                ...last,
                                reasoningActive: false,
                                reasoningEndedAt: Date.now(),
                            }];
                        }
                        return prev;
                    });
                } else if (data.type === 'chat_tool_call_start' && data.conversation_id === convId) {
                    // v6.4.32: foldable tool-call badge instead of dumping
                    // args/result into chat content.
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.streaming) {
                            const calls = Array.isArray(last.toolCalls) ? last.toolCalls : [];
                            const newCall: ToolCallTrace = {
                                id: String(data.id || `tc_${Date.now()}`),
                                name: String(data.name || ''),
                                argsPreview: data.args_preview ? String(data.args_preview) : undefined,
                                args: data.args ? String(data.args) : undefined,
                                pending: true,
                            };
                            return [...prev.slice(0, -1), {
                                ...last,
                                toolCalls: [...calls, newCall],
                            }];
                        }
                        return prev;
                    });
                } else if (data.type === 'chat_tool_call_result' && data.conversation_id === convId) {
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.streaming) {
                            const calls = Array.isArray(last.toolCalls) ? last.toolCalls : [];
                            const updated = calls.map(c => c.id === data.id ? {
                                ...c,
                                pending: false,
                                success: typeof data.success === 'boolean' ? data.success : c.success,
                                preview: data.preview ? String(data.preview) : c.preview,
                                resultTruncated: data.result_truncated ? String(data.result_truncated) : c.resultTruncated,
                            } : c);
                            return [...prev.slice(0, -1), { ...last, toolCalls: updated }];
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
                } else if (data.type === 'chat_heartbeat' && data.conversation_id === convId) {
                    // v6.4.34: backend still alive, just busy. UI can show
                    // a "思考中… XXs" indicator. For now we just mark the
                    // last assistant message so ReasoningBlock's active
                    // state keeps the spinner visible.
                    setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.streaming && !last.reasoning) {
                            return [...prev.slice(0, -1), {
                                ...last,
                                reasoningActive: true,
                                reasoningStartedAt: last.reasoningStartedAt || Date.now(),
                                reasoning: last.reasoning || '（长任务进行中…）',
                            }];
                        }
                        return prev;
                    });
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
        const rawText = input.trim();
        if (!rawText || streaming || !connected) return;

        // v6.4.30: slash commands — expand to full instructions
        // so the AI gets an unambiguous directive regardless of its
        // system prompt length. Pattern from Cursor / Claude Code.
        const expandSlashCommand = (raw: string): string => {
            const lower = raw.toLowerCase().trim();
            if (lower.startsWith('/read ')) {
                const path = raw.slice(6).trim();
                return `Read \`${path}\` using file_ops and summarize what's in it. Include any TODOs/stubs/bugs you spot.`;
            }
            if (lower.startsWith('/edit ')) {
                const rest = raw.slice(6).trim();
                return `Make the following change in the Active Project: ${rest}. First file_ops read the target file, then file_ops edit with a precise old_string→new_string.`;
            }
            if (lower.startsWith('/fix')) {
                const rest = raw.slice(4).trim();
                const desc = rest.startsWith(' ') ? rest.slice(1) : rest;
                return desc
                    ? `Fix this issue in the Active Project: ${desc}. Discover the relevant file first (file_ops read the primary artifact), locate the defect, then file_ops edit to repair it surgically. Do NOT ask me for the file path.`
                    : `Audit the Active Project for obvious bugs. Start by file_ops read on the primary artifact, enumerate the top 3-5 issues you'd fix, then wait for my confirmation before editing.`;
            }
            if (lower.startsWith('/screenshot') || lower.startsWith('/preview')) {
                return `Open http://127.0.0.1:8765/preview/ via the browser tool, navigate + screenshot it, and tell me what's on screen. Note any JS errors from browser diagnostics.`;
            }
            if (lower.startsWith('/diff')) {
                const file = raw.slice(5).trim() || '';
                return file
                    ? `Show me a diff of the latest changes to ${file} (compare with git if available, else use file_ops read + explain recent modifications).`
                    : `Summarize what files were changed in this session and show a condensed diff.`;
            }
            if (lower.startsWith('/find ')) {
                const q = raw.slice(6).trim();
                return `Use file_ops search to find occurrences of "${q}" in the Active Project. Report file:line for each match.`;
            }
            if (lower === '/help' || lower === '/?') {
                // Handled locally, short-circuit
                return '__LOCAL_HELP__';
            }
            return raw;
        };
        let text = expandSlashCommand(rawText);
        if (text === '__LOCAL_HELP__') {
            setMessages(prev => [...prev, { role: 'user', content: rawText, ts: Date.now() }]);
            setMessages(prev => [...prev, {
                role: 'system',
                content: `Available slash commands:
/fix [描述]        — audit or fix the current project (auto-discovers source)
/read <path>       — read and summarize a file
/edit <描述>       — make a precise edit (auto-reads the file first)
/screenshot        — navigate to preview and screenshot
/find <query>      — search text across the project
/diff [path]       — show recent changes
/help              — this message`,
                ts: Date.now(),
            }]);
            setInput('');
            return;
        }

        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;

        // Add user message (raw text — we keep the slash command visible)
        setMessages(prev => [...prev, { role: 'user', content: rawText, ts: Date.now() }]);

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
        if (sessionId) persistMessages(sessionId, []);
    }, [sessionId]);

    const handleStop = useCallback(() => {
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'chat_stop' }));
        }
        // v6.4.36 (maintainer): flip the last assistant message to
        // NOT streaming locally so subsequent chat_token events (which
        // may still arrive before backend acks the stop) are ignored by
        // the WS handler (it checks last?.streaming before appending).
        // Without this, UI keeps scrolling even after user clicks stop.
        setMessages(prev => {
            const last = prev[prev.length - 1];
            if (last?.streaming) {
                return [...prev.slice(0, -1), {
                    ...last,
                    streaming: false,
                    reasoningActive: false,
                    reasoningEndedAt: Date.now(),
                    content: (last.content || '') + '\n\n[已停止]',
                }];
            }
            return prev;
        });
        streamBufferRef.current = '';
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
                    {modelOptions.map(m => (
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
                                background: 'rgba(91,140,255,0.15)', border: '1px solid rgba(91,140,255,0.2)', color: '#c4d9ff',
                            } : msg.role === 'system' ? {
                                background: 'rgba(248,81,73,0.12)', border: '1px solid rgba(248,81,73,0.2)', color: '#ffa8a8',
                            } : {
                                background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)', color: 'var(--text1, #eee)',
                            }),
                        }}>
                            {msg.role === 'assistant' && msg.model && (
                                <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 4, fontWeight: 500 }}>{msg.model}</div>
                            )}
                            {/* v6.4.29: collapsible thinking bubble above main content */}
                            {msg.role === 'assistant' && (msg.reasoning || msg.reasoningActive) && (
                                <ReasoningBlock
                                    text={msg.reasoning || ''}
                                    active={!!msg.reasoningActive}
                                    startedAt={msg.reasoningStartedAt}
                                    endedAt={msg.reasoningEndedAt}
                                />
                            )}
                            {/* v6.4.32: foldable tool-call badges */}
                            {msg.role === 'assistant' && Array.isArray(msg.toolCalls) && msg.toolCalls.length > 0 && (
                                <div style={{ marginBottom: 6 }}>
                                    {msg.toolCalls.map(tc => (
                                        <ToolCallBlock key={tc.id} call={tc} />
                                    ))}
                                </div>
                            )}
                            {/* v6.4.55 UX: live "正在编写/读取/访问" status bar
                                while a tool call is still pending. Looks at
                                the latest pending tool call and derives a
                                natural-language label from its action. */}
                            {msg.role === 'assistant' && msg.streaming && Array.isArray(msg.toolCalls) && (() => {
                                const pendingTc = [...msg.toolCalls].reverse().find(t => t.pending);
                                if (!pendingTc) return null;
                                let parsedAction = '';
                                let parsedPath = '';
                                try {
                                    const a = pendingTc.args || pendingTc.argsPreview || '';
                                    const p = a ? JSON.parse(a) : {};
                                    parsedAction = String(p.action || '').toLowerCase();
                                    parsedPath = String(p.path || p.url || p.selector || '');
                                } catch { /* ignore */ }
                                const fileName = parsedPath ? parsedPath.split('/').slice(-1)[0] : '';
                                const labels: Record<string, string> = {
                                    write: `正在编写 ${fileName || '文件'}…`,
                                    edit: `正在修改 ${fileName || '文件'}…`,
                                    read: `正在读取 ${fileName || '文件'}…`,
                                    list: '正在列目录…',
                                    search: `正在搜索 ${parsedPath ? `「${parsedPath}」` : ''}…`,
                                    delete: `正在删除 ${fileName || '文件'}…`,
                                    navigate: `正在访问 ${parsedPath || '网页'}…`,
                                    observe: '正在观察页面…',
                                    click: `正在点击 ${parsedPath || '元素'}…`,
                                    screenshot: '正在截图…',
                                    press: '正在按键…',
                                    fill: '正在填写表单…',
                                };
                                const label = labels[parsedAction] || `正在执行 ${pendingTc.name}${parsedAction ? '.' + parsedAction : ''}…`;
                                const isWrite = parsedAction === 'write' || parsedAction === 'edit';
                                return (
                                    <div style={{
                                        margin: '4px 0 6px 0',
                                        padding: '4px 10px',
                                        fontSize: 11,
                                        color: isWrite ? '#2ea043' : 'var(--color-muted, #8a8a93)',
                                        background: isWrite ? 'rgba(46,160,67,0.08)' : 'rgba(168,85,247,0.04)',
                                        border: isWrite ? '1px solid rgba(46,160,67,0.3)' : '1px solid rgba(168,85,247,0.15)',
                                        borderRadius: 12,
                                        display: 'inline-flex',
                                        alignItems: 'center',
                                        gap: 6,
                                        fontWeight: 500,
                                    }}>
                                        <span style={{
                                            display: 'inline-block',
                                            width: 8, height: 8,
                                            borderRadius: '50%',
                                            background: isWrite ? '#2ea043' : '#a855f7',
                                            animation: 'toolcall-spin 1.2s linear infinite',
                                        }} />
                                        {label}
                                    </div>
                                );
                            })()}
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
                            background: !connected || !input.trim() ? 'rgba(91,140,255,0.3)' : 'rgba(91,140,255,0.8)',
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
