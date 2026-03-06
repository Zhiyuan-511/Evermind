'use client';

import { ChatMessage, SubTask } from '@/lib/types';
import { useState, useRef, useEffect } from 'react';

interface ChatPanelProps {
    messages: ChatMessage[];
    onSendGoal: (goal: string) => void;
    connected: boolean;
    running: boolean;
    onStop: () => void;
    lang: 'en' | 'zh';
}

export default function ChatPanel({ messages, onSendGoal, connected, running, onStop, lang }: ChatPanelProps) {
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
        <div className="glass-strong flex flex-col h-full border-l border-white/5" style={{ width: '320px' }}>
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
                                <div dangerouslySetInnerHTML={{ __html: msg.content }} />
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

            {/* Input area */}
            <div className="p-3 border-t border-white/5">
                {running && (
                    <button onClick={onStop} className="btn btn-danger w-full mb-2 text-[10px] justify-center">
                        ⏹ {lang === 'zh' ? '停止执行' : 'Stop execution'}
                    </button>
                )}
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
