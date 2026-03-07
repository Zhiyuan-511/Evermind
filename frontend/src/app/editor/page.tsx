'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
    ReactFlow, MiniMap, Controls, Background, BackgroundVariant,
    addEdge, useNodesState, useEdgesState,
    type Connection, type Node, type Edge as RFEdge,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import Sidebar from '@/components/Sidebar';
import Toolbar from '@/components/Toolbar';
import ChatPanel from '@/components/ChatPanel';
import AgentNode from '@/components/AgentNode';
import { useWebSocket } from '@/hooks/useWebSocket';
import { NODE_TYPES, type ChatMessage } from '@/lib/types';

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8765/ws';
const THEME_STORAGE_KEY = 'evermind-theme';

let nodeCounter = 0;
function genId() { return 'n_' + (++nodeCounter) + '_' + Date.now().toString(36); }
function now() { return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); }

// React Flow node types
const nodeTypes = { agent: AgentNode };

export default function EditorPage() {
    const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState<RFEdge>([]);
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [workflowName, setWorkflowName] = useState('Workflow 1');
    const [lang, setLang] = useState<'en' | 'zh'>('en');
    const [theme, setTheme] = useState<'dark' | 'light'>(() => {
        if (typeof window === 'undefined') return 'dark';
        try {
            const savedTheme = window.localStorage.getItem(THEME_STORAGE_KEY) as 'dark' | 'light' | null;
            return savedTheme === 'light' ? 'light' : 'dark';
        } catch {
            return 'dark';
        }
    });
    const [running, setRunning] = useState(false);
    const [draggingType, setDraggingType] = useState<string | null>(null);

    useEffect(() => {
        document.documentElement.dataset.theme = theme;
        try {
            window.localStorage.setItem(THEME_STORAGE_KEY, theme);
        } catch {
            // ignore localStorage write issues
        }
    }, [theme]);

    // ── Chat message helper ──
    const addMessage = useCallback((role: 'user' | 'system' | 'agent', content: string, sender?: string, icon?: string, borderColor?: string) => {
        setMessages(prev => [...prev, {
            id: Date.now().toString(36) + Math.random().toString(36).slice(2),
            role, content, sender, icon, borderColor,
            timestamp: now(),
        }]);
    }, []);

    // ── WebSocket ──
    const onWSMessage = useCallback((msg: Record<string, unknown>) => {
        const t = msg.type as string;

        if (t === 'orchestrator_start') {
            setRunning(true);
            addMessage('system', '🧠 Analyzing goal and creating plan...', 'Orchestrator', '🧠');
        } else if (t === 'plan_created') {
            const subtasks = (msg.subtasks as Array<{ agent: string; task: string }>) || [];
            const lines = subtasks.map(st => `▸ <b>${st.agent}</b>: ${(st.task || '').substring(0, 80)}`).join('<br/>');
            addMessage('system', `📋 Plan (${msg.total} subtasks):<br/>${lines}`, 'Plan', '📋');
        } else if (t === 'subtask_start') {
            addMessage('agent', `⚡ Working: ${((msg.task as string) || '').substring(0, 100)}`, `${msg.agent} #${msg.subtask_id}`, '⚡');
        } else if (t === 'subtask_retry') {
            addMessage('system', `🔄 Retry (attempt ${msg.retry}/${msg.max_retries})`, `Retry #${msg.subtask_id}`, '🔄', 'var(--yellow)');
        } else if (t === 'test_failed_retrying') {
            addMessage('system', '🔴 Tests failed → re-running with fix instructions', 'Tester', '🧪', 'var(--red)');
        } else if (t === 'orchestrator_complete') {
            setRunning(false);
            const success = msg.success as boolean;
            addMessage('system',
                `${success ? '✅' : '⚠️'} <b>${msg.completed}/${msg.total_subtasks}</b> done${(msg.total_retries as number) > 0 ? ` (${msg.total_retries} retries)` : ''} in ${msg.duration_seconds}s`,
                'Report', '🏁', success ? 'var(--green)' : 'var(--orange)');
        } else if (t === 'orchestrator_error') {
            setRunning(false);
            addMessage('system', `❌ Error: ${msg.error}`, 'Error', '❌', 'var(--red)');
        } else if (t === 'node_start') {
            setNodes(nds => nds.map(n =>
                n.id === msg.node_id ? { ...n, data: { ...n.data, status: 'running', progress: 0 } } : n
            ));
        } else if (t === 'node_complete') {
            setNodes(nds => nds.map(n =>
                n.id === msg.node_id ? { ...n, data: { ...n.data, status: msg.success ? 'done' : 'error', progress: 100 } } : n
            ));
        }
    }, [addMessage, setNodes]);

    const { connected, sendGoal, runWorkflow, stop } = useWebSocket({ url: WS_URL, onMessage: onWSMessage });

    // ── Connect edges ──
    const onConnect = useCallback((conn: Connection) => {
        setEdges(eds => addEdge({ ...conn, animated: true, style: { stroke: 'var(--edge-color)', strokeWidth: 2 } }, eds));
    }, [setEdges]);

    // ── Drag from sidebar ──
    const handleSidebarDragStart = useCallback((type: string) => {
        setDraggingType(type);
    }, []);

    const onDragOver = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
    }, []);

    const onDrop = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        const type = draggingType;
        if (!type) return;
        const info = NODE_TYPES[type];
        if (!info) return;

        const reactFlowBounds = (e.target as HTMLElement).closest('.react-flow')?.getBoundingClientRect();
        if (!reactFlowBounds) return;
        const x = e.clientX - reactFlowBounds.left;
        const y = e.clientY - reactFlowBounds.top;

        const newNode: Node = {
            id: genId(),
            type: 'agent',
            position: { x, y },
            data: {
                nodeType: type,
                label: lang === 'zh' ? info.label_zh : info.label_en,
                status: 'idle',
                progress: 0,
                model: 'gpt-5.4',
                lastOutput: '',
                lang,
            },
        };
        setNodes(nds => [...nds, newNode]);
        setDraggingType(null);
    }, [draggingType, lang, setNodes]);

    // ── Send goal ──
    const handleSendGoal = useCallback((goal: string) => {
        addMessage('user', goal, 'You', '👤');
        if (connected) {
            sendGoal(goal);
            addMessage('system', '🧠 Goal received — planning...', 'Evermind', '🧠');
        } else {
            addMessage('system', '🔴 Backend offline. Run: <code>cd backend && python server.py</code>', 'System', '⚠️');
        }
    }, [connected, sendGoal, addMessage]);

    // ── Toolbar actions ──
    const handleRun = () => { if (connected) runWorkflow(nodes, edges); };
    const handleStop = () => { stop(); setRunning(false); };
    const handleExport = () => {
        const data = { name: workflowName, nodes, edges, exported_at: new Date().toISOString(), version: '2.0' };
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url; a.download = `${workflowName.replace(/\s+/g, '_')}.json`; a.click();
    };
    const handleClear = () => { setNodes([]); setEdges([]); };
    const handleThemeToggle = () => setTheme(current => current === 'dark' ? 'light' : 'dark');

    // ── Edge style ──
    const defaultEdgeOptions = useMemo(() => ({
        animated: true,
        style: { stroke: 'var(--edge-color)', strokeWidth: 2 },
    }), []);

    return (
        <div className="flex h-screen">
            <Sidebar onDragStart={handleSidebarDragStart} connected={connected} lang={lang} />

            <div className="flex-1 flex flex-col">
                <Toolbar
                    workflowName={workflowName} onNameChange={setWorkflowName}
                    onRun={handleRun} onStop={handleStop} onExport={handleExport} onClear={handleClear}
                    running={running} connected={connected} lang={lang} onLangToggle={() => setLang(l => l === 'en' ? 'zh' : 'en')}
                    theme={theme} onThemeToggle={handleThemeToggle}
                />

                <div className="flex flex-1 overflow-hidden">
                    {/* React Flow Canvas */}
                    <div className="flex-1" onDragOver={onDragOver} onDrop={onDrop}>
                        <ReactFlow
                            nodes={nodes}
                            edges={edges}
                            onNodesChange={onNodesChange}
                            onEdgesChange={onEdgesChange}
                            onConnect={onConnect}
                            nodeTypes={nodeTypes}
                            defaultEdgeOptions={defaultEdgeOptions}
                            snapToGrid
                            snapGrid={[16, 16]}
                            defaultViewport={{ x: 0, y: 0, zoom: 1 }}
                            proOptions={{ hideAttribution: true }}
                            style={{ background: 'var(--canvas-bg)' }}
                        >
                            <Background variant={BackgroundVariant.Dots} gap={24} size={1} color={'var(--canvas-dot)'} />
                            <Controls />
                            <MiniMap
                                nodeColor={(n) => {
                                    const nt = n.data?.nodeType as string;
                                    return NODE_TYPES[nt]?.color || '#666';
                                }}
                                maskColor="var(--minimap-mask)"
                            />
                        </ReactFlow>
                    </div>

                    <ChatPanel
                        messages={messages}
                        onSendGoal={handleSendGoal}
                        connected={connected}
                        running={running}
                        onStop={handleStop}
                        lang={lang}
                    />
                </div>
            </div>
        </div>
    );
}
