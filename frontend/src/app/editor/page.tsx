'use client';

import { useState, useEffect, useMemo } from 'react';
import {
    ReactFlow, MiniMap, Controls, Background, BackgroundVariant,
    type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import Sidebar from '@/components/Sidebar';
import Toolbar from '@/components/Toolbar';
import ChatPanel from '@/components/ChatPanel';
import AgentNode from '@/components/AgentNode';
import SettingsModal from '@/components/SettingsModal';
import TemplateGallery from '@/components/TemplateGallery';
import GuideModal from '@/components/GuideModal';
import HistoryModal from '@/components/HistoryModal';
import DiagnosticsModal from '@/components/DiagnosticsModal';
import ArtifactsModal from '@/components/ArtifactsModal';
import ReportsModal from '@/components/ReportsModal';
import TaskBoardModal from '@/components/TaskBoardModal';
import PreviewCenter from '@/components/PreviewCenter';
import { NODE_TYPES } from '@/lib/types';

import { useChatHistory } from '@/hooks/useChatHistory';
import { useRunReports } from '@/hooks/useRunReports';
import { useWorkflowState } from '@/hooks/useWorkflowState';
import { useRuntimeConnection } from '@/hooks/useRuntimeConnection';
import { TaskRunProvider, useTaskContext, useRunContext } from '@/contexts/TaskRunProvider';

const THEME_STORAGE_KEY = 'evermind-theme';

const nodeTypes = { agent: AgentNode };

function stripCacheBust(url: string): string {
    return url
        .replace(/([?&])_ts=\d+(&?)/, (_m, p1: string, p2: string) => (p1 === '?' && p2 ? '?' : p1))
        .replace(/[?&]$/, '');
}

function withCacheBust(url: string): string {
    const clean = stripCacheBust(url);
    const sep = clean.includes('?') ? '&' : '?';
    return `${clean}${sep}_ts=${Date.now()}`;
}

export default function EditorPage() {
    return (
        <TaskRunProvider>
            <EditorPageInner />
        </TaskRunProvider>
    );
}

function EditorPageInner() {
    // ── Theme + Lang ──
    const [lang, setLang] = useState<'en' | 'zh'>('zh');
    const [theme, setTheme] = useState<'dark' | 'light'>(() => {
        if (typeof window === 'undefined') return 'dark';
        try {
            const saved = window.localStorage.getItem(THEME_STORAGE_KEY) as 'dark' | 'light' | null;
            return saved === 'light' ? 'light' : 'dark';
        } catch { return 'dark'; }
    });
    const [wsUrl, setWsUrl] = useState(process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8765/ws');
    const [difficulty, setDifficulty] = useState<'simple' | 'standard' | 'pro'>('standard');

    useEffect(() => {
        document.documentElement.dataset.theme = theme;
        try { window.localStorage.setItem(THEME_STORAGE_KEY, theme); } catch { /* ignore */ }
    }, [theme]);

    // ── P0-1: Canonical state from context ──
    const taskCtx = useTaskContext();
    const runCtx = useRunContext();
    const reconnectRunIds = useMemo(
        () => runCtx.runs
            .filter((run) => run.runtime === 'openclaw' && run.status === 'running')
            .map((run) => run.id),
        [runCtx.runs],
    );

    // ── Hooks ──
    const chat = useChatHistory(lang);
    const reports = useRunReports();
    const workflow = useWorkflowState(lang);
    const runtime = useRuntimeConnection({
        wsUrl, lang, difficulty,
        messages: chat.messages,
        addMessage: chat.addMessage,
        addReport: reports.addReport,
        buildPlanNodes: workflow.buildPlanNodes,
        updateNodeData: workflow.updateNodeData,
        nodes: workflow.nodes,
        edges: workflow.edges,
        setNodes: workflow.setNodes as (nodes: Node[] | ((prev: Node[]) => Node[])) => void,
        // P0-1: WS events → canonical state merge
        onMergeTask: taskCtx.mergeTask,
        onMergeRun: runCtx.mergeRun,
        onMergeNodeExecution: runCtx.mergeNodeExecution,
        reconnectRunIds,
    });

    // ── Modal states ──
    const [settingsOpen, setSettingsOpen] = useState(false);
    const [templatesOpen, setTemplatesOpen] = useState(false);
    const [guideOpen, setGuideOpen] = useState(false);
    const [historyOpen, setHistoryOpen] = useState(false);
    const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
    const [artifactsOpen, setArtifactsOpen] = useState(false);
    const [reportsOpen, setReportsOpen] = useState(false);
    const [taskBoardOpen, setTaskBoardOpen] = useState(false);

    const handleThemeToggle = () => setTheme(current => current === 'dark' ? 'light' : 'dark');

    const previewTaskTitle = useMemo(() => {
        const latestUserGoal = [...chat.messages]
            .reverse()
            .find((message) => message.role === 'user' && message.content.trim());
        return latestUserGoal?.content.trim().slice(0, 120);
    }, [chat.messages]);

    const defaultEdgeOptions = useMemo(() => ({
        animated: true,
        style: { stroke: 'var(--edge-color)', strokeWidth: 2 },
    }), []);

    return (
        <div className="flex h-screen relative">
            <Sidebar
                onDragStart={workflow.handleSidebarDragStart}
                connected={runtime.connected}
                lang={lang}
                onOpenArtifacts={() => setArtifactsOpen(true)}
                onOpenReports={() => setReportsOpen(true)}
                onOpenTaskBoard={() => setTaskBoardOpen(true)}
            />

            <div className="flex-1 flex flex-col">
                <Toolbar
                    workflowName={chat.workflowName}
                    onNameChange={chat.handleWorkflowNameChange}
                    onRun={runtime.handleRun}
                    onStop={runtime.handleStop}
                    onExport={() => workflow.handleExport(chat.workflowName)}
                    onClear={workflow.handleClear}
                    running={runtime.running}
                    connected={runtime.connected}
                    lang={lang}
                    onLangToggle={() => setLang(l => l === 'en' ? 'zh' : 'en')}
                    theme={theme}
                    onThemeToggle={handleThemeToggle}
                    onOpenSettings={() => setSettingsOpen(true)}
                    onOpenTemplates={() => setTemplatesOpen(true)}
                    onOpenGuide={() => setGuideOpen(true)}
                    onOpenHistory={() => setHistoryOpen(true)}
                    onOpenDiagnostics={() => setDiagnosticsOpen(true)}
                    canvasView={runtime.canvasView}
                    onToggleCanvasView={() => runtime.setCanvasView(v => v === 'editor' ? 'preview' : 'editor')}
                    hasPreview={!!runtime.previewUrl}
                />

                <div className="flex flex-1 overflow-hidden">
                    <div className="flex-1 relative" onDragOver={workflow.onDragOver} onDrop={workflow.onDrop}>
                        {runtime.canvasView === 'editor' ? (
                            <ReactFlow
                                nodes={workflow.nodes}
                                edges={workflow.edges}
                                onNodesChange={workflow.onNodesChange}
                                onEdgesChange={workflow.onEdgesChange}
                                onConnect={workflow.onConnect}
                                nodeTypes={nodeTypes}
                                defaultEdgeOptions={defaultEdgeOptions}
                                deleteKeyCode="Backspace"
                                snapToGrid snapGrid={[16, 16]}
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
                        ) : (
                            <PreviewCenter
                                previewUrl={runtime.previewUrl}
                                onRefresh={() => runtime.setPreviewUrl((current) => current ? withCacheBust(current) : current)}
                                onClose={() => runtime.setCanvasView('editor')}
                                onNewWindow={() => runtime.previewUrl && window.open(stripCacheBust(runtime.previewUrl), '_blank', 'noopener,noreferrer')}
                                lang={lang}
                                runId={runtime.previewRunId || undefined}
                                taskTitle={previewTaskTitle}
                                running={runtime.running}
                            />
                        )}
                    </div>
                    <ChatPanel messages={chat.messages} onSendGoal={runtime.handleSendGoal} connected={runtime.connected} running={runtime.running} onStop={runtime.handleStop} lang={lang} difficulty={difficulty} onDifficultyChange={setDifficulty} />
                </div>
            </div>

            {/* Modals */}
            <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} lang={lang} onLangChange={setLang} theme={theme} onThemeChange={setTheme} connected={runtime.connected} wsUrl={wsUrl} onWsUrlChange={setWsUrl} wsRef={runtime.wsRef} />
            <TemplateGallery open={templatesOpen} onClose={() => setTemplatesOpen(false)} onLoadTemplate={workflow.handleLoadTemplate} lang={lang} />
            <GuideModal open={guideOpen} onClose={() => setGuideOpen(false)} lang={lang} />
            <HistoryModal
                open={historyOpen} onClose={() => setHistoryOpen(false)} lang={lang}
                sessions={chat.historySessions} activeSessionId={chat.activeSessionId}
                onSelectSession={(id) => { chat.handleSelectSession(id); runtime.setPreviewUrl(null); runtime.setCanvasView('editor'); setHistoryOpen(false); }}
                onCreateSession={() => { chat.handleCreateSession(); runtime.setPreviewUrl(null); runtime.setCanvasView('editor'); setHistoryOpen(false); }}
                onDeleteSession={(id) => { chat.handleDeleteSession(id); runtime.setPreviewUrl(null); runtime.setCanvasView('editor'); }}
                onRenameSession={chat.handleRenameSession}
            />
            <DiagnosticsModal open={diagnosticsOpen} onClose={() => setDiagnosticsOpen(false)} lang={lang} />
            <ArtifactsModal open={artifactsOpen} onClose={() => setArtifactsOpen(false)} lang={lang} />
            <ReportsModal open={reportsOpen} onClose={() => setReportsOpen(false)} lang={lang} reports={reports.runReports} onDeleteReport={reports.deleteReport} onClearReports={reports.clearReports} />
            <TaskBoardModal open={taskBoardOpen} onClose={() => setTaskBoardOpen(false)} lang={lang} runReports={reports.runReports} />
        </div>
    );
}
