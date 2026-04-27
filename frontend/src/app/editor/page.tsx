'use client';
import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import {
    ReactFlow, MiniMap, Controls, Background, BackgroundVariant,
    type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import Sidebar from '@/components/Sidebar';
import Toolbar from '@/components/Toolbar';
import ChatPanel from '@/components/ChatPanel';
import DirectChatPanel from '@/components/DirectChatPanel';
import CodeEditorPanel from '@/components/CodeEditorPanel';
import MonacoEditor from '@monaco-editor/react';
import '@/lib/monaco-env'; // Electron-compatible Monaco worker setup
import type { OpenFile } from '@/components/CodeEditorPanel';
import AgentNode from '@/components/AgentNode';
import SettingsModal from '@/components/SettingsModal';
import GitHubModal from '@/components/GitHubModal';
import TemplateGallery from '@/components/TemplateGallery';
import SkillsLibraryModal from '@/components/SkillsLibraryModal';
import LessonsModal from '@/components/LessonsModal';
import GuideModal from '@/components/GuideModal';
import HistoryModal from '@/components/HistoryModal';
import DiagnosticsModal from '@/components/DiagnosticsModal';
import ArtifactsModal from '@/components/ArtifactsModal';
import ReportsModal from '@/components/ReportsModal';
import NodeDetailPopup from '@/components/NodeDetailPopup';
import PreviewCenter from '@/components/PreviewCenter';
import OpenClawPanel from '@/components/OpenClawPanel';
import WelcomeWizard, { shouldShowWelcomeWizard, markWelcomeWizardSeen } from '@/components/WelcomeWizard';
import type { ConnectorEvent } from '@/components/OpenClawPanel';
import { NODE_TYPES } from '@/lib/types';
import { OPENCLAW_UI_ENABLED, normalizeRuntimeModeForDisplay } from '@/lib/runtimeDisplay';

import { useChatHistory } from '@/hooks/useChatHistory';
import { useRunReports } from '@/hooks/useRunReports';
import { useWorkflowState } from '@/hooks/useWorkflowState';
import { useRuntimeConnection } from '@/hooks/useRuntimeConnection';
import { TaskRunProvider, useTaskContext, useRunContext } from '@/contexts/TaskRunProvider';

const THEME_STORAGE_KEY = 'evermind-theme';
const RUNTIME_STORAGE_KEY = 'evermind-runtime';
const nodeTypes = { agent: AgentNode };
const ACTIVE_RUN_STATUSES = new Set(['queued', 'running', 'waiting_review', 'waiting_selfcheck']);
const HYDRATABLE_TASK_STATUSES = new Set(['executing', 'review', 'selfcheck', 'done', 'completed']);
const HYDRATION_ACTIVE_STATUSES = new Set(['running', 'queued', 'waiting_review', 'waiting_selfcheck', 'executing']);
const TERMINAL_NE_STATUSES = new Set(['passed', 'failed', 'skipped', 'cancelled', 'done', 'completed']);

function isActiveRunStatus(status?: string | null): boolean {
    return ACTIVE_RUN_STATUSES.has(String(status || '').trim().toLowerCase());
}

function isHydratableTaskStatus(status?: string | null): boolean {
    return HYDRATABLE_TASK_STATUSES.has(String(status || '').trim().toLowerCase());
}

function normalizeHydrationNodeType(value: unknown): string {
    const raw = String(value || '').trim().toLowerCase();
    if (!raw) return 'builder';
    if (NODE_TYPES[raw]) return raw;

    const withoutNumericSuffix = raw.replace(/(?:[_-]?\d+)+$/, '');
    if (withoutNumericSuffix && NODE_TYPES[withoutNumericSuffix]) return withoutNumericSuffix;

    const alphaPrefix = raw.match(/^[a-z]+/)?.[0] || '';
    if (alphaPrefix && NODE_TYPES[alphaPrefix]) return alphaPrefix;

    return raw;
}

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

function normalizeEpochMs(value: unknown, fallback = 0): number {
    const num = Number(value || 0);
    if (!Number.isFinite(num) || num <= 0) return fallback;
    return num < 10_000_000_000 ? Math.round(num * 1000) : Math.round(num);
}

function deriveDurationSeconds(startedAt: unknown, endedAt: unknown): number | undefined {
    const startedMs = normalizeEpochMs(startedAt, 0);
    const endedMs = normalizeEpochMs(endedAt, 0);
    if (!startedMs || !endedMs || endedMs < startedMs) return undefined;
    return Math.max(0, Math.round((endedMs - startedMs) / 1000));
}

function normalizeNodeLog(log: unknown): Array<{ ts: number; msg: string; type: string }> {
    if (!Array.isArray(log)) return [];
    return log.reduce<Array<{ ts: number; msg: string; type: string }>>((acc, item) => {
        if (!item || typeof item !== 'object') return acc;
        const record = item as Record<string, unknown>;
        const msg = String(record.msg || '').trim();
        if (!msg) return acc;
        acc.push({
            ts: normalizeEpochMs(record.ts, Date.now()),
            msg: msg.slice(0, 520),
            type: String(record.type || 'info'),
        });
        return acc.slice(-80);
    }, []);
}

function findWorkflowNodeByRuntimeIdentity(
    nodes: Node[],
    options: {
        nodeExecutionId?: string;
        rawNodeKey?: string;
        normalizedType?: string;
        label?: string;
    },
): Node | undefined {
    const nodeExecutionId = String(options.nodeExecutionId || '').trim();
    const rawNodeKey = String(options.rawNodeKey || '').trim().toLowerCase();
    const normalizedType = String(options.normalizedType || '').trim().toLowerCase();
    const label = String(options.label || '').trim();

    if (nodeExecutionId) {
        const exactExecutionMatch = nodes.find((node) =>
            String(node.data?.nodeExecutionId || '').trim() === nodeExecutionId,
        );
        if (exactExecutionMatch) return exactExecutionMatch;
    }

    if (rawNodeKey) {
        const exactKeyMatches = nodes.filter((node) => {
            const nodeType = String(node.data?.nodeType || '').trim().toLowerCase();
            const storedRawNodeKey = String(node.data?.rawNodeKey || '').trim().toLowerCase();
            return storedRawNodeKey === rawNodeKey || nodeType === rawNodeKey;
        });
        if (exactKeyMatches.length === 1) return exactKeyMatches[0];
    }

    if (rawNodeKey && normalizedType && rawNodeKey === normalizedType) {
        const normalizedMatches = nodes.filter((node) => {
            const nodeType = String(node.data?.nodeType || '').trim().toLowerCase();
            const storedRawNodeKey = String(node.data?.rawNodeKey || '').trim().toLowerCase();
            return nodeType === normalizedType || storedRawNodeKey === normalizedType;
        });
        if (normalizedMatches.length === 1) return normalizedMatches[0];
    }

    if (label) {
        const labelMatches = nodes.filter((node) =>
            String(node.data?.label || '').trim() === label,
        );
        if (labelMatches.length === 1) return labelMatches[0];
    }

    return undefined;
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
    const [wsUrl, setWsUrl] = useState(process.env.NEXT_PUBLIC_WS_URL || 'ws://127.0.0.1:8765/ws');
    const [difficulty, setDifficulty] = useState<'simple' | 'standard' | 'pro' | 'ultra' | 'custom'>('standard');
    const [chatMode, setChatMode] = useState<'pipeline' | 'direct'>('pipeline');
    // ── Right panel resizable width ──
    const [rightPanelWidth, setRightPanelWidth] = useState(() => {
        if (typeof window === 'undefined') return 380;
        try { return Number(window.localStorage.getItem('evermind-rpw')) || 380; } catch { return 380; }
    });
    const [resizing, setResizing] = useState(false);
    // Keep the live width in a ref so the drag handler can read it without
    // being rebuilt every time the state changes (the old behaviour caused a
    // "must widen before narrowing" glitch because the handler closure stayed
    // pinned to the initial width after the first mousemove re-render).
    const rightPanelWidthRef = useRef(380);
    useEffect(() => { rightPanelWidthRef.current = rightPanelWidth; }, [rightPanelWidth]);
    // ── File viewer state (Cursor-style IDE mode) ──
    const [openFiles, setOpenFiles] = useState<OpenFile[]>([]);
    const [activeFileIndex, setActiveFileIndex] = useState(0);
    const [selectedRuntime, setSelectedRuntime] = useState<'local' | 'openclaw'>(() => {
        if (!OPENCLAW_UI_ENABLED) return 'local';
        if (typeof window === 'undefined') return 'local';
        try {
            const saved = window.localStorage.getItem(RUNTIME_STORAGE_KEY);
            return saved === 'openclaw' ? 'openclaw' : 'local';
        } catch {
            return 'local';
        }
    });
    const effectiveSelectedRuntime: 'local' | 'openclaw' = OPENCLAW_UI_ENABLED ? selectedRuntime : 'local';

    // §2.1: Read env query param for DEV/PACKAGED badge
    const envTag = useMemo(() => {
        if (typeof window === 'undefined') return '';
        try {
            return new URLSearchParams(window.location.search).get('env') || '';
        } catch { return ''; }
    }, []);

    useEffect(() => {
        document.documentElement.dataset.theme = theme;
        try { window.localStorage.setItem(THEME_STORAGE_KEY, theme); } catch { /* ignore */ }
    }, [theme]);

    useEffect(() => {
        try { window.localStorage.setItem(RUNTIME_STORAGE_KEY, effectiveSelectedRuntime); } catch { /* ignore */ }
    }, [effectiveSelectedRuntime]);

    // ── Right panel resize handlers ──
    const handleResizeStart = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        setResizing(true);
        const startX = e.clientX;
        const startW = rightPanelWidthRef.current;
        const onMove = (ev: MouseEvent) => {
            const delta = startX - ev.clientX; // dragging left = wider
            const next = Math.max(300, Math.min(700, startW + delta));
            setRightPanelWidth(next);
        };
        const onUp = () => {
            setResizing(false);
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            try { window.localStorage.setItem('evermind-rpw', String(rightPanelWidthRef.current)); } catch { /* ignore */ }
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
    }, []);

    // (File viewer handlers defined after runtime declaration below)

    // ── P0-1: Canonical state from context ──
    const taskCtx = useTaskContext();
    const runCtx = useRunContext();
    const {
        tasks,
        selectedTask,
        fetchTasks,
        selectTask,
        mergeTask,
    } = taskCtx;
    const {
        runs,
        selectedRun,
        latestRun,
        nodeExecutions,
        fetchRuns,
        fetchNodeExecutions,
        selectRun,
        mergeRun,
        mergeNodeExecution,
    } = runCtx;
    const reconnectRunIds = useMemo(
        () => runs
            .filter((run) => OPENCLAW_UI_ENABLED && run.runtime === 'openclaw' && run.status === 'running')
            .map((run) => run.id),
        [runs],
    );
    const preferredTaskForHydration = useMemo(() => (
        [...tasks]
            .filter((task) => isHydratableTaskStatus(task.status))
            .sort((a, b) => b.updatedAt - a.updatedAt)[0] || null
    ), [tasks]);
    const preferredRunForHydration = useMemo(() => {
        const activeOpenClawRuns = runs
            .filter((run) => OPENCLAW_UI_ENABLED && run.runtime === 'openclaw' && isActiveRunStatus(run.status))
            .sort((a, b) => b.updated_at - a.updated_at);
        if (activeOpenClawRuns.length > 0) return activeOpenClawRuns[0];

        const activeRuns = runs
            .filter((run) => isActiveRunStatus(run.status))
            .sort((a, b) => b.updated_at - a.updated_at);
        return activeRuns[0] || null;
    }, [runs]);
    const activeRun = useMemo(() => {
        const activeOpenClawRuns = runs
            .filter((run) => OPENCLAW_UI_ENABLED && run.runtime === 'openclaw' && isActiveRunStatus(run.status))
            .sort((a, b) => b.updated_at - a.updated_at);
        if (activeOpenClawRuns.length > 0) return activeOpenClawRuns[0];

        const activeRuns = runs
            .filter((run) => isActiveRunStatus(run.status))
            .sort((a, b) => b.updated_at - a.updated_at);
        if (activeRuns.length > 0) return activeRuns[0];

        if (selectedRun) return selectedRun;
        return latestRun;
    }, [latestRun, runs, selectedRun]);
    const summaryTask = useMemo(() => {
        if (activeRun?.task_id) {
            const linked = tasks.find((task) => task.id === activeRun.task_id);
            if (linked) return linked;
        }
        return preferredTaskForHydration;
    }, [activeRun, preferredTaskForHydration, tasks]);
    const activeRunNodeExecutions = useMemo(() => {
        if (!activeRun) return [];
        return nodeExecutions.filter((node) => node.run_id === activeRun.id);
    }, [activeRun, nodeExecutions]);
    const isRouterWarmup = useMemo(() => {
        if (!activeRun) return false;
        if (String(activeRun.status || '').trim().toLowerCase() !== 'running') return false;
        if (String(activeRun.current_node_execution_id || '').trim()) return false;
        if ((activeRun.active_node_execution_ids || []).length > 0) return false;
        if (activeRunNodeExecutions.length === 0) return false;
        return activeRunNodeExecutions.every((node) => String(node.status || '').trim().toLowerCase() === 'queued');
    }, [activeRun, activeRunNodeExecutions]);
    const summaryActiveNodeLabels = useMemo(() => {
        if (!activeRun || selectedRun?.id !== activeRun.id) {
            return isRouterWarmup ? [lang === 'zh' ? '路由 / 规划准备中' : 'Router / planning'] : [];
        }
        const activeIds = activeRun.active_node_execution_ids || [];
        const labels = activeIds
            .map((id) => nodeExecutions.find((node) => node.id === id))
            .filter(Boolean)
            .map((node) => node!.node_label || node!.node_key)
            .filter(Boolean);
        if (labels.length === 0 && isRouterWarmup) {
            return [lang === 'zh' ? '路由 / 规划准备中' : 'Router / planning'];
        }
        return [...new Set(labels)];
    }, [activeRun, isRouterWarmup, lang, nodeExecutions, selectedRun]);
    const summaryRunningNodes = useMemo(() => {
        if (!activeRun || selectedRun?.id !== activeRun.id) return isRouterWarmup ? 1 : 0;
        const count = activeRunNodeExecutions.filter(
            (node) => String(node.status || '').trim().toLowerCase() === 'running',
        ).length;
        return count > 0 ? count : (isRouterWarmup ? 1 : 0);
    }, [activeRun, activeRunNodeExecutions, isRouterWarmup, selectedRun]);
    const summaryCompletedNodes = useMemo(() => {
        if (!activeRun || selectedRun?.id !== activeRun.id) return 0;
        return nodeExecutions.filter((node) =>
            node.run_id === activeRun.id && TERMINAL_NE_STATUSES.has(String(node.status || '').trim().toLowerCase())
        ).length;
    }, [activeRun, nodeExecutions, selectedRun]);
    const summaryTotalNodes = activeRun?.node_execution_ids?.length
        || (activeRun ? nodeExecutions.filter(ne => ne.run_id === activeRun.id).length : 0);

    // ── Hooks ──
    const chat = useChatHistory(lang);
    const reports = useRunReports();
    const workflow = useWorkflowState(lang);
    const [connectorPanelOpen, setConnectorPanelOpen] = useState(false);
    const [connectorEvents, setConnectorEvents] = useState<ConnectorEvent[]>([]);
    const appendConnectorEvent = useCallback((event: Omit<ConnectorEvent, 'id'>) => {
        setConnectorEvents((prev) => {
            const last = prev[prev.length - 1];
            const duplicate = last
                && last.type === event.type
                && last.label === event.label
                && last.detail === event.detail
                && Math.abs(last.timestamp - event.timestamp) < 1200;
            if (duplicate) return prev;

            const nextEvent: ConnectorEvent = {
                id: `${event.type}_${event.timestamp}_${Math.random().toString(36).slice(2, 8)}`,
                ...event,
            };
            return [...prev, nextEvent].slice(-80);
        });
    }, []);
    const runtime = useRuntimeConnection({
        wsUrl, lang, difficulty, goalRuntime: effectiveSelectedRuntime, sessionId: chat.activeSessionId,
        messages: chat.messages,
        addMessage: chat.addMessage,
        addReport: reports.addReport,
        buildPlanNodes: workflow.buildPlanNodes,
        updateNodeData: workflow.updateNodeData,
        nodes: workflow.nodes,
        edges: workflow.edges,
        setNodes: workflow.setNodes as (nodes: Node[] | ((prev: Node[]) => Node[])) => void,
        // P0-1: WS events → canonical state merge
        onMergeTask: mergeTask,
        onMergeRun: mergeRun,
        onMergeNodeExecution: mergeNodeExecution,
        reconnectRunIds,
        onConnectorEvent: appendConnectorEvent,
    });
    const workflowNodes = workflow.nodes;
    const buildPlanNodes = workflow.buildPlanNodes;
    const updateNodeData = workflow.updateNodeData;
    const runtimeConnected = runtime.connected;

    // ── File viewer handlers ──
    const handleOpenFileInEditor = useCallback(async (filePath: string, rootFolder?: string, content?: string, ext?: string) => {
        const existing = openFiles.findIndex(f => f.path === filePath);
        if (existing >= 0) {
            setActiveFileIndex(existing);
            runtime.setCanvasView('files');
            return;
        }
        let fileContent = content || '';
        const fileExt = ext || filePath.split('.').pop() || '';
        const fileName = filePath.split('/').pop() || filePath;
        if (!fileContent) {
            try {
                const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
                const params = new URLSearchParams({ path: filePath });
                if (rootFolder) params.set('root', rootFolder);
                const res = await fetch(`${apiBase}/api/workspace/file?${params}`);
                if (res.ok) {
                    const data = await res.json();
                    fileContent = data.content || '';
                }
            } catch { /* ignore */ }
        }
        const newFile: OpenFile = { path: filePath, name: fileName, content: fileContent, ext: fileExt, rootFolder: rootFolder || '' };
        setOpenFiles(prev => [...prev, newFile]);
        setActiveFileIndex(openFiles.length);
        runtime.setCanvasView('files');
    }, [openFiles, runtime]);

    const handleCloseFileInEditor = useCallback((index: number) => {
        setOpenFiles(prev => prev.filter((_, i) => i !== index));
        setActiveFileIndex(prev => prev >= index && prev > 0 ? prev - 1 : prev);
    }, []);

    const handleSwitchFile = useCallback((index: number) => {
        setActiveFileIndex(index);
    }, []);

    const handleUpdateFileContent = useCallback((index: number, content: string) => {
        setOpenFiles(prev => prev.map((f, i) => i === index ? { ...f, content, modified: true } : f));
    }, []);

    const handleSaveFile = useCallback(async (file: OpenFile) => {
        const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
        try {
            const res = await fetch(`${apiBase}/api/workspace/write`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: file.path, root: file.rootFolder || '', content: file.content }),
            });
            if (res.ok) {
                setOpenFiles(prev => prev.map(f => f.path === file.path ? { ...f, modified: false } : f));
            }
        } catch { /* ignore */ }
    }, []);
    const runtimePreviewUrl = runtime.previewUrl;
    const setCanvasView = runtime.setCanvasView;

    // §2.4: Auto-fetch tasks when WS connects (hydrate state even if events were missed)
    useEffect(() => {
        if (runtimeConnected) {
            void fetchTasks();
        }
    }, [fetchTasks, runtimeConnected]);

    // Refresh runs for the selected task when the WS reconnects so missed transitions hydrate immediately.
    useEffect(() => {
        if (!runtimeConnected) return;
        if (!selectedTask?.id) return;
        void fetchRuns();
    }, [fetchRuns, runtimeConnected, selectedTask?.id]);

    // v7.5: when launchpad navigates here with ?task=<id>, that user choice
    // wins over the auto-refocus heuristic. Without this guard, clicking a
    // Recent task whose status was `done`/`failed`/`cancelled` got immediately
    // replaced by `preferredTaskForHydration` (the most active task) — user
    // saw "I clicked task A but the editor opened task B".
    const urlTaskOverrideRef = useRef<string | null>(null);

    // If the current selection is stale or inactive after reconnect, refocus the most active task.
    useEffect(() => {
        if (!runtimeConnected) return;
        if (urlTaskOverrideRef.current) return; // URL choice locks selection until user navigates again
        if (!preferredTaskForHydration?.id) return;
        if (selectedTask?.id === preferredTaskForHydration.id) return;
        if (!selectedTask || !isHydratableTaskStatus(selectedTask.status)) {
            selectTask(preferredTaskForHydration.id);
        }
    }, [preferredTaskForHydration?.id, runtimeConnected, selectTask, selectedTask, selectedTask?.id, selectedTask?.status]);

    // v7.5/v7.6: when the selected task changes, sync chat panel + canvas
    // to that task. Was missing — clicking Recent task A on launchpad
    // changed selectedTask to A but chat panel still showed previous
    // active session AND canvas kept old task's nodes, so the user saw
    // "B's chat with B's nodes" after clicking A. Two-agent audit
    // (af4ff6edfd147ce01 + a4fae0fdf56433665) cross-validated:
    //   1) handleSelectSession was silent no-op when sessionId not in
    //      localStorage (fixed in useChatHistory.ts).
    //   2) selectedRun could be left pointing at a different task's
    //      active run via preferredRunForHydration.
    //   3) workflow.nodes were not cleared on task switch.
    // This effect now: (a) synthesises a stable task-bound chat session
    // id when task has none, (b) clears canvas, (c) drops selectedRun so
    // the next effect re-picks within THIS task only.
    const lastTaskChatSyncRef = useRef<string>('');
    const hydrationDoneRef = useRef<string>(''); // declared early so we can reset it on task switch
    useEffect(() => {
        if (!selectedTask?.id) return;
        if (lastTaskChatSyncRef.current === selectedTask.id) return;
        lastTaskChatSyncRef.current = selectedTask.id;
        const rawSid = String((selectedTask as any).sessionId || (selectedTask as any).session_id || '').trim();
        const tsid = rawSid || `task-${selectedTask.id}`; // synthesise stable id for sessionless tasks
        try { chat.handleSelectSession(tsid, selectedTask.title); } catch { /* ignore */ }
        try { workflow.handleClear(); } catch { /* ignore */ } // drop prior task's canvas residue
        hydrationDoneRef.current = ''; // allow re-hydration for the new run
        // v7.7: selectRun(null) was unconditional — created a deadlock when
        // the new task already had a valid run that just hadn't propagated
        // yet (selectedRun = null → preferredRun gate rejects → stays null
        // forever → hydration effect never fires → canvas blank). Now: only
        // null when current selection is from a different task.
        if (selectedRun && selectedRun.task_id !== selectedTask.id) {
            try { selectRun(null); } catch { /* ignore */ }
        }
    }, [selectedTask?.id, selectedTask?.title, chat, workflow, selectRun, selectedRun]);

    // Once runs are available, auto-select the active run when the current selection is stale.
    // v7.6: never cross-task — preferred run MUST belong to the currently
    // selected task. Previous code relied on `selectedRunMatchesTask` AFTER
    // already deciding to swap, but if `selectedRun` was null (initial load)
    // it would happily pick a foreign-task active run.
    useEffect(() => {
        if (!runtimeConnected) return;
        if (!selectedTask?.id) return;
        if (!preferredRunForHydration?.id) return;
        if (preferredRunForHydration.task_id && preferredRunForHydration.task_id !== selectedTask.id) return;
        if (selectedRun?.id === preferredRunForHydration.id) return;
        const selectedRunMatchesTask = selectedRun?.task_id === selectedTask.id;
        if (!selectedRun || !isActiveRunStatus(selectedRun.status) || !selectedRunMatchesTask) {
            selectRun(preferredRunForHydration.id);
        }
    }, [preferredRunForHydration?.id, preferredRunForHydration?.task_id, runtimeConnected, selectRun, selectedRun, selectedRun?.id, selectedRun?.status, selectedRun?.task_id, selectedTask?.id]);

    // Re-pull node executions for the selected run after reconnect so the timeline/canvas catch up immediately.
    useEffect(() => {
        if (!runtimeConnected) return;
        if (!selectedRun?.id) return;
        void fetchNodeExecutions(selectedRun.id);
    }, [fetchNodeExecutions, runtimeConnected, selectedRun?.id]);

    // §3.1: Rebuild canvas from existing NEs ONLY when reconnecting or restoring state.
    // Skip when canvas already has agent nodes (plan_created already built them).
    // This prevents hydration from overwriting live status updates (e.g., subtask_start → running).
    // v7.6: hydrationDoneRef declared earlier (line ~511) so the task-switch
    // effect can reset it. No re-declaration here.
    useEffect(() => {
        if (!selectedRun?.id) return;
        const runId = selectedRun.id;
        const runStatus = String(selectedRun.status || '').trim().toLowerCase();
        // v7.5: was `if (!HYDRATION_ACTIVE_STATUSES.has(runStatus)) return;` —
        // that meant clicking a Recent task whose run was `done`/`failed`/
        // `cancelled` left the canvas empty (or with the default planner
        // node), so the user couldn't see what nodes ran or their results.
        // Now we also hydrate terminal runs IF the canvas has no agent
        // nodes linked to this run yet — purely for read-only display.
        const isTerminal = !HYDRATION_ACTIVE_STATUSES.has(runStatus);
        if (isTerminal) {
            const linked = workflowNodes.some(
                (n) => n.type === 'agent' && nodeExecutions.some(
                    (ne) => ne.run_id === runId && String(ne.id || '').trim() === String(n.data?.nodeExecutionId || '').trim()
                )
            );
            if (linked) return; // already showing this run, leave it alone
        }

        // If we already hydrated this run, don't rebuild — live events handle updates
        if (hydrationDoneRef.current === runId) return;

        const runNEs = nodeExecutions.filter(ne => ne.run_id === selectedRun?.id);
        if (runNEs.length === 0) return;
        const synthesizeWarmupRunning = runStatus === 'running'
            && !String(selectedRun.current_node_execution_id || '').trim()
            && (selectedRun.active_node_execution_ids || []).length === 0
            && runNEs.every((ne) => String(ne.status || '').trim().toLowerCase() === 'queued');

        // Debounce slightly to let React state settle after NE fetch
        const timer = setTimeout(() => {
            const existingAgentNodes = workflowNodes.filter((n) => n.type === 'agent');
            const hasLinkedCurrentRunNodes = existingAgentNodes.some((node) => {
                const nodeExecutionId = String(node.data?.nodeExecutionId || '').trim();
                return runNEs.some((ne) => String(ne.id || '').trim() === nodeExecutionId);
            });

            let nodeMap: Record<string, string> = {};
            if (existingAgentNodes.length === 0 || !hasLinkedCurrentRunNodes) {
                const subtaskFormat = runNEs.map(ne => ({
                    id: String(ne.id || ''),
                    agent: normalizeHydrationNodeType(ne.node_key || 'builder'),
                    task: String(ne.node_label || ne.node_key || ''),
                    depends_on: Array.isArray(ne.depends_on_keys) ? ne.depends_on_keys.map(String) : [],
                }));
                const keyToId: Record<string, string> = {};
                for (const ne of runNEs) {
                    if (ne.node_key && ne.id) keyToId[String(ne.node_key)] = String(ne.id);
                }
                const subtasksWithIdDeps = subtaskFormat.map(st => ({
                    ...st,
                    depends_on: st.depends_on.map(key => keyToId[key] || key),
                }));
                nodeMap = buildPlanNodes(subtasksWithIdDeps, lang);
            }

            for (const ne of runNEs) {
                const neId = String(ne.id || '').trim();
                const nodeKey = String(ne.node_key || '').trim().toLowerCase();
                const normalizedType = normalizeHydrationNodeType(ne.node_key || 'builder');
                const isWarmupLeadNode = synthesizeWarmupRunning && neId === String(runNEs[0]?.id || '').trim();
                const canvasNodeId = nodeMap[neId]
                    || findWorkflowNodeByRuntimeIdentity(workflowNodes, {
                        nodeExecutionId: neId,
                        rawNodeKey: nodeKey,
                        normalizedType,
                        label: String(ne.node_label || '').trim(),
                    })?.id;
                if (!canvasNodeId) continue;
                const startedAt = normalizeEpochMs(ne.started_at, 0);
                const endedAt = normalizeEpochMs(ne.ended_at, 0);
                const durationSeconds = deriveDurationSeconds(startedAt, endedAt);
                updateNodeData(canvasNodeId, {
                    nodeExecutionId: String(ne.id || ''),
                    rawNodeKey: String(ne.node_key || ''),
                    nodeType: normalizeHydrationNodeType(ne.node_key || 'builder'),
                    label: String(ne.node_label || ne.node_key || ''),
                    status: isWarmupLeadNode ? 'running' : String(ne.status || 'queued'),
                    runtime: selectedRun?.runtime === 'openclaw' ? 'openclaw' : 'local',
                    ...(Number.isFinite(Number(ne.progress)) ? { progress: Math.max(0, Math.min(100, Number(ne.progress))) } : (isWarmupLeadNode ? { progress: 5 } : {})),
                    ...(String(ne.assigned_model || '').trim() ? { assignedModel: String(ne.assigned_model || '').trim() } : {}),
                    ...(String(ne.input_summary || '').trim() ? { taskDescription: String(ne.input_summary || '').trim() } : {}),
                    ...(String(ne.output_summary || '').trim()
                        ? {
                            outputSummary: String(ne.output_summary || '').trim(),
                            lastOutput: String(ne.output_summary || '').trim(),
                        }
                        : {}),
                    ...(Array.isArray(ne.loaded_skills) && ne.loaded_skills.length > 0 ? { loadedSkills: ne.loaded_skills.map(String) } : {}),
                    ...(Array.isArray(ne.activity_log) && ne.activity_log.length > 0 ? { log: normalizeNodeLog(ne.activity_log) } : {}),
                    ...(Number.isFinite(Number(ne.tokens_used)) ? { tokensUsed: Number(ne.tokens_used) } : {}),
                    ...(Number.isFinite(Number(ne.cost)) ? { cost: Number(ne.cost) } : {}),
                    ...(startedAt > 0 ? { startedAt } : {}),
                    ...(endedAt > 0 ? { endedAt } : {}),
                    ...(durationSeconds !== undefined ? { durationSeconds } : {}),
                    ...(String(ne.phase || '').trim()
                        ? { phase: String(ne.phase || '').trim() }
                        : (isWarmupLeadNode ? { phase: 'routing' } : {})),
                });
            }
            hydrationDoneRef.current = runId;
        }, 150);
        return () => clearTimeout(timer);
    }, [buildPlanNodes, lang, nodeExecutions, selectedRun, selectedRun?.id, selectedRun?.runtime, selectedRun?.status, updateNodeData, workflowNodes]);

    // §3.5b: Auto-switch to preview once a completed run also has a ready preview.
    const previewAutoSwitchStateRef = useRef<{ runId: string; status: string; hadPreview: boolean }>({
        runId: '',
        status: '',
        hadPreview: false,
    });
    useEffect(() => {
        const runId = String(selectedRun?.id || '');
        const runStatus = String(selectedRun?.status || '');
        const hasPreview = Boolean(runtimePreviewUrl);
        const prev = previewAutoSwitchStateRef.current;

        const sameRun = prev.runId === runId;
        const justCompletedWithPreview = sameRun
            && runStatus === 'done'
            && prev.status !== 'done'
            && hasPreview;
        const previewBecameReadyAfterDone = sameRun
            && runStatus === 'done'
            && prev.status === 'done'
            && !prev.hadPreview
            && hasPreview;

        if (justCompletedWithPreview || previewBecameReadyAfterDone) {
            setCanvasView('preview');
        }

        previewAutoSwitchStateRef.current = {
            runId,
            status: runStatus,
            hadPreview: hasPreview,
        };
    }, [runtimePreviewUrl, selectedRun?.id, selectedRun?.status, setCanvasView]);

    // ── Modal states ──
    const [settingsOpen, setSettingsOpen] = useState(false);
    const [githubOpen, setGithubOpen] = useState(false);
    // v7.1i (maintainer 2026-04-25): CLI mode toggle status — drives whether
    // the "Ultra" difficulty button is enabled or dimmed/disabled.
    const [cliEnabled, setCliEnabled] = useState(false);
    useEffect(() => {
        const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
        const refreshCli = async () => {
            try {
                const r = await fetch(`${apiBase}/api/settings`, { credentials: 'omit' });
                if (r.ok) {
                    const d = await r.json();
                    setCliEnabled(Boolean((d?.cli_mode || {}).enabled));
                }
            } catch { /* keep default false */ }
        };
        refreshCli();
        // Poll every 4s so toggle from Settings reflects in <5s.
        const id = setInterval(refreshCli, 4000);
        return () => clearInterval(id);
    }, [settingsOpen]);
    // If user disables CLI mode while Ultra is selected, downgrade to Pro.
    useEffect(() => {
        if (!cliEnabled && difficulty === 'ultra') {
            setDifficulty('pro');
        }
    }, [cliEnabled, difficulty]);
    // v6.2 (maintainer 2026-04-20): welcome wizard shown on first run, guarded by
    // localStorage.evermind_onboarded_v62. Skip button marks seen.
    const [wizardOpen, setWizardOpen] = useState(false);
    useEffect(() => {
        if (shouldShowWelcomeWizard()) {
            setWizardOpen(true);
        }
    }, []);
    const [templatesOpen, setTemplatesOpen] = useState(false);

    // v7.2 (maintainer 2026-04-26): respond to ?panel= URL param so launchpad
    // links can deep-link straight into Templates / GitHub / Settings panels
    // instead of dropping users on a bare canvas.
    useEffect(() => {
        if (typeof window === 'undefined') return;
        const params = new URLSearchParams(window.location.search);
        const panel = (params.get('panel') || '').toLowerCase().trim();
        if (panel === 'templates') {
            setTemplatesOpen(true);
        } else if (panel === 'github' || panel === 'clone') {
            setGithubOpen(true);
        } else if (panel === 'settings') {
            setSettingsOpen(true);
        }
        // v7.5: also honour `?task=<id>` so launchpad → Recent → editor
        // actually opens that task's run/canvas (was: param ignored, editor
        // showed an empty canvas regardless of which Recent item was clicked).
        // urlTaskOverrideRef locks the selection so the auto-refocus effect
        // doesn't immediately replace it with `preferredTaskForHydration`.
        const taskParam = (params.get('task') || '').trim();
        if (taskParam) {
            urlTaskOverrideRef.current = taskParam;
            try { selectTask(taskParam); } catch { /* selectTask hydrates async */ }
        }
        // Strip the param from the URL so refreshing the page doesn't
        // re-open the modal indefinitely.
        if (panel || taskParam) {
            const url = new URL(window.location.href);
            url.searchParams.delete('panel');
            url.searchParams.delete('task');
            window.history.replaceState({}, '', url.toString());
        }
    }, []);
    // v7.3 (maintainer 2026-04-26) — per-task workspace banner. When the selected
    // task has zero files in its isolated workspace, show a non-blocking
    // banner inviting the user to add input files. Uses sessionStorage to
    // remember dismissals so we don't pester users on every tab return.
    const [workspaceBanner, setWorkspaceBanner] = useState<
        | { taskId: string; fileCount: number; path: string; dismissed: boolean }
        | null
    >(null);
    useEffect(() => {
        const tid = (selectedTask as any)?.id;
        if (!tid || typeof window === 'undefined') {
            setWorkspaceBanner(null);
            return;
        }
        const dismissedKey = `evermind:ws-banner-dismissed:${tid}`;
        const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
        const ctrl = new AbortController();
        let cancelled = false;
        const probe = async () => {
            if (cancelled) return;
            try {
                // v7.3.3 (maintainer 2026-04-26) — banner detection now correctly
                // queries both stores:
                //   1. task-scoped: GET /api/tasks/<id>/workspace (file_count)
                //   2. global FileExplorer: GET /api/workspace/roots → resolve
                //      "output" root path → GET /api/workspace/tree?root=<path>
                //      (server expects an absolute path, not the symbolic
                //      name "output"). Earlier code passed "?root=output"
                //      which the server tried to Path.resolve() — that
                //      always 404'd, making the banner falsely report 0
                //      files even when the user had already added files.
                const [taskRes, rootsRes] = await Promise.allSettled([
                    fetch(`${apiBase}/api/tasks/${encodeURIComponent(tid)}/workspace`, { signal: ctrl.signal }),
                    fetch(`${apiBase}/api/workspace/roots`, { signal: ctrl.signal }),
                ]);
                let taskFileCount = 0;
                let taskPath = '';
                if (taskRes.status === 'fulfilled' && taskRes.value.ok) {
                    const tj = await taskRes.value.json();
                    taskFileCount = Number(tj?.stats?.file_count || 0);
                    taskPath = String(tj?.path || '');
                }
                // v7.3.5 (maintainer 2026-04-26) — banner now also recognises the
                // `artifact_sync_dir` (Delivery Folder) configured via
                // FileExplorerPanel's "添加文件夹". Earlier code only walked
                // `output_dir` which is the runtime build directory, NOT the
                // user-uploaded source folder. Observed: user added
                // `/path/to/Desktop/测试` as Delivery Folder ✅ but
                // banner kept saying "workspace empty" because output_dir
                // had no user files. Now: any of (a) task-scoped files,
                // (b) artifact_sync_dir set + has files, (c) any custom
                // user-added root in `folders[]` with files → banner hides.
                let globalUserFileCount = 0;
                if (rootsRes.status === 'fulfilled' && rootsRes.value.ok) {
                    const rootsJson = await rootsRes.value.json();
                    // Hidden underscore-prefixed dirs are always Evermind system dirs.
                    const SYSTEM_PREFIXES = [
                        '_evermind_runtime', '_previous_run_', '_stable_previews',
                        '_browser_records', '_visual_regression',
                    ];
                    // v7.3.9 audit-fix CRITICAL — `task_` was too greedy; user
                    // folders like `task_manager`, `task_app`, `task_tracker`
                    // would be hidden, banner falsely sticks at 0. Match ONLY
                    // the orchestrator's runtime shape: `task_<int>` (single
                    // digit / small int; orchestrator uses task_1, task_5,
                    // task_12 etc. for SubTask.id). Real user folders almost
                    // always include letters or longer strings.
                    const SYSTEM_TASK_RE = /^task_\d{1,4}$/;
                    const isSystemDir = (name: string) =>
                        SYSTEM_PREFIXES.some((p) => name.startsWith(p)) ||
                        SYSTEM_TASK_RE.test(name);
                    // Bounded recursion protects against pathological cyclic JSON.
                    const walk = (nodes: any[], depth: number = 0): number => {
                        if (depth > 20) return 0;
                        let n = 0;
                        for (const node of (nodes || [])) {
                            const name = String(node?.name || '');
                            if (isSystemDir(name)) continue;
                            if (node?.type === 'file' || node?.kind === 'file') n += 1;
                            else if (Array.isArray(node?.children)) n += walk(node.children, depth + 1);
                        }
                        return n;
                    };
                    // Build the union of paths to probe: output_dir + every
                    // entry in folders[]. Skip system-controlled `runtime_output`
                    // because those auto-populate during a run and would
                    // false-trigger the banner once a run starts.
                    const pathsToProbe: { path: string; alwaysCount: boolean }[] = [];
                    const outputPath = String(rootsJson?.output_dir || rootsJson?.output || '').trim();
                    if (outputPath) pathsToProbe.push({ path: outputPath, alwaysCount: false });
                    const folders: any[] = Array.isArray(rootsJson?.folders) ? rootsJson.folders : [];
                    for (const f of folders) {
                        const fp = String(f?.path || '').trim();
                        if (!fp) continue;
                        // User-added folders (artifact_sync, workspace, custom)
                        // are de-facto evidence the user already set up a
                        // workspace — count them as ≥1 even if currently empty,
                        // so the banner hides as soon as the user clicks "添加文件夹".
                        const isUserAdded = (f?.kind || '') !== 'runtime_output';
                        if (isUserAdded) {
                            // First, count actual files inside; if 0, still
                            // grant +1 because the folder being registered IS
                            // the user's intent.
                            pathsToProbe.push({ path: fp, alwaysCount: true });
                        }
                    }
                    // Probe each path in parallel; sum file counts.
                    const treeResults = await Promise.allSettled(
                        pathsToProbe.map((p) => fetch(
                            `${apiBase}/api/workspace/tree?root=${encodeURIComponent(p.path)}`,
                            { signal: ctrl.signal },
                        )),
                    );
                    for (let i = 0; i < treeResults.length; i++) {
                        const tr = treeResults[i];
                        if (tr.status !== 'fulfilled') continue;
                        try {
                            if (!tr.value.ok) {
                                // Folder may not exist yet (just registered).
                                // alwaysCount entries still count as 1.
                                if (pathsToProbe[i].alwaysCount) globalUserFileCount += 1;
                                continue;
                            }
                            const gj = await tr.value.json();
                            const tree = Array.isArray(gj?.tree) ? gj.tree : [];
                            const fc = walk(tree);
                            globalUserFileCount += Math.max(fc, pathsToProbe[i].alwaysCount ? 1 : 0);
                        } catch {
                            if (pathsToProbe[i].alwaysCount) globalUserFileCount += 1;
                        }
                    }
                }
                const fc = taskFileCount + globalUserFileCount;
                // v7.3.3 audit fix CRITICAL: re-check cancelled after awaits
                // to prevent setState on unmounted component / wrong-task race.
                if (cancelled) return;
                // v7.3.3 audit fix MAJOR-1: when files appear, clear the
                // sessionStorage dismissed flag so the banner reappears
                // properly if the user later deletes everything back to 0.
                if (fc > 0) {
                    window.sessionStorage.removeItem(dismissedKey);
                }
                const dismissed = window.sessionStorage.getItem(dismissedKey) === '1';
                setWorkspaceBanner({
                    taskId: tid,
                    fileCount: fc,
                    path: taskPath,
                    dismissed,
                });
            } catch { /* ignore */ }
        };
        // Initial probe + re-probe every 8s so the banner reflects newly
        // uploaded files (from FileExplorer or banner button) within seconds
        // instead of requiring a full editor re-mount.
        // v7.3.9 audit-fix MAJOR — gate the polling on `document.visibilityState`.
        // Without it, a backgrounded editor tab fires 6+ requests every 8s
        // (=2700/h per tab), wasting network + relay budget.
        probe();
        const intervalProbe = () => {
            if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
                probe();
            }
        };
        const interval = setInterval(intervalProbe, 8000);
        return () => {
            cancelled = true;
            clearInterval(interval);
            ctrl.abort();
        };
    }, [(selectedTask as any)?.id]);

    const dismissWorkspaceBanner = () => {
        if (workspaceBanner && typeof window !== 'undefined') {
            window.sessionStorage.setItem(`evermind:ws-banner-dismissed:${workspaceBanner.taskId}`, '1');
            setWorkspaceBanner({ ...workspaceBanner, dismissed: true });
        }
    };

    const triggerAddFiles = () => {
        if (!workspaceBanner) return;
        // Use a hidden input to let the user pick local files; uploaded
        // contents are POSTed to /api/tasks/<id>/workspace/upload.
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.style.display = 'none';
        input.onchange = async () => {
            const files = Array.from(input.files || []);
            if (!files.length) return;
            const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
            const payloads: any[] = [];
            for (const f of files.slice(0, 32)) {
                try {
                    const buf = await f.arrayBuffer();
                    const bin = btoa(
                        Array.from(new Uint8Array(buf))
                            .map((b) => String.fromCharCode(b))
                            .join(''),
                    );
                    payloads.push({ name: f.name, encoding: 'base64', content: bin });
                } catch { /* skip */ }
            }
            if (!payloads.length) return;
            try {
                const r = await fetch(`${apiBase}/api/tasks/${encodeURIComponent(workspaceBanner.taskId)}/workspace/upload`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ files: payloads }),
                });
                const j = await r.json();
                if (j?.ok) {
                    setWorkspaceBanner({ ...workspaceBanner, fileCount: workspaceBanner.fileCount + (j.saved?.length || 0) });
                }
            } catch { /* ignore */ }
        };
        document.body.appendChild(input);
        input.click();
        setTimeout(() => input.remove(), 0);
    };

    const [skillsLibraryOpen, setSkillsLibraryOpen] = useState(false);
    const [lessonsOpen, setLessonsOpen] = useState(false);
    const [guideOpen, setGuideOpen] = useState(false);
    const [historyOpen, setHistoryOpen] = useState(false);
    const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
    const [artifactsOpen, setArtifactsOpen] = useState(false);
    const [reportsOpen, setReportsOpen] = useState(false);
    const [nodeDetailOpen, setNodeDetailOpen] = useState(false);
    const [selectedNodeSnapshot, setSelectedNodeSnapshot] = useState<Record<string, unknown> | null>(null);
    const [openedFile, setOpenedFile] = useState<{ path: string; root: string; content: string; ext: string } | null>(null);

    const handleOpenFile = useCallback((filePath: string, root: string, content: string, ext: string) => {
        setOpenedFile({ path: filePath, root, content, ext });
    }, []);

    const buildNodeDetailSnapshot = useCallback((rawData: Record<string, unknown>) => {
        const nodeExecutionId = String(rawData.nodeExecutionId || '').trim();
        const rawNodeKey = String(rawData.rawNodeKey || rawData.nodeType || '').trim().toLowerCase();
        const canonicalNode = runCtx.nodeExecutions.find((item) => {
            if (nodeExecutionId && item.id === nodeExecutionId) return true;
            return rawNodeKey && String(item.node_key || '').trim().toLowerCase() === rawNodeKey;
        });
        const startedAt = normalizeEpochMs(rawData.startedAt ?? canonicalNode?.started_at, 0);
        const endedAt = normalizeEpochMs(rawData.endedAt ?? canonicalNode?.ended_at, 0);
        const durationSeconds = Number.isFinite(Number(rawData.durationSeconds))
            ? Math.max(0, Number(rawData.durationSeconds))
            : deriveDurationSeconds(startedAt, endedAt);
        const outputSummary = String(rawData.outputSummary || canonicalNode?.output_summary || '').trim();
        const lastOutput = String(rawData.lastOutput || canonicalNode?.output_summary || '').trim();
        return {
            ...rawData,
            ...(nodeExecutionId ? { nodeExecutionId } : {}),
            ...(canonicalNode?.node_label ? { label: rawData.label || canonicalNode.node_label } : {}),
            ...(canonicalNode?.node_key ? { rawNodeKey: canonicalNode.node_key } : {}),
            ...(canonicalNode?.assigned_model ? { assignedModel: canonicalNode.assigned_model } : {}),
            ...(canonicalNode?.input_summary ? { taskDescription: rawData.taskDescription || canonicalNode.input_summary } : {}),
            ...(outputSummary ? { outputSummary } : {}),
            ...(lastOutput ? { lastOutput } : {}),
            ...(Array.isArray(canonicalNode?.loaded_skills) && canonicalNode.loaded_skills.length > 0
                ? { loadedSkills: Array.isArray(rawData.loadedSkills) && rawData.loadedSkills.length > 0 ? rawData.loadedSkills : canonicalNode.loaded_skills }
                : {}),
            ...(Number.isFinite(Number(canonicalNode?.tokens_used)) ? { tokensUsed: Number(canonicalNode?.tokens_used) } : {}),
            ...(Number.isFinite(Number(canonicalNode?.cost)) ? { cost: Number(canonicalNode?.cost) } : {}),
            ...(startedAt > 0 ? { startedAt } : {}),
            ...(endedAt > 0 ? { endedAt } : {}),
            ...(durationSeconds !== undefined ? { durationSeconds } : {}),
            ...(String(canonicalNode?.phase || '').trim() ? { phase: String(canonicalNode?.phase || '').trim() } : {}),
            ...(Array.isArray(canonicalNode?.reference_urls) && canonicalNode.reference_urls.length > 0
                ? { referenceUrls: canonicalNode.reference_urls }
                : {}),
            ...(String(canonicalNode?.current_action || '').trim() ? { currentAction: String(canonicalNode?.current_action || '').trim() } : {}),
            ...(Array.isArray(canonicalNode?.work_summary) && canonicalNode.work_summary.length > 0
                ? { workSummary: canonicalNode.work_summary }
                : {}),
            ...(canonicalNode?.tool_call_stats && Object.keys(canonicalNode.tool_call_stats).length > 0
                ? { toolCallStats: canonicalNode.tool_call_stats }
                : {}),
            ...(String(canonicalNode?.blocking_reason || '').trim() ? { blockingReason: String(canonicalNode?.blocking_reason || '').trim() } : {}),
            ...(String(canonicalNode?.latest_review_decision || '').trim() ? { latestReviewDecision: String(canonicalNode?.latest_review_decision || '').trim() } : {}),
            ...(Array.isArray(canonicalNode?.artifact_ids) && canonicalNode.artifact_ids.length > 0
                ? { artifactIds: canonicalNode.artifact_ids }
                : {}),
            ...(Array.isArray(canonicalNode?.report_artifact_ids) && canonicalNode.report_artifact_ids.length > 0
                ? { reportArtifactIds: canonicalNode.report_artifact_ids }
                : {}),
            ...(Array.isArray(canonicalNode?.handoff_artifact_ids) && canonicalNode.handoff_artifact_ids.length > 0
                ? { handoffArtifactIds: canonicalNode.handoff_artifact_ids }
                : {}),
            ...(String(canonicalNode?.dossier_artifact_id || '').trim() ? { dossierArtifactId: String(canonicalNode?.dossier_artifact_id || '').trim() } : {}),
            ...(String(canonicalNode?.summary_artifact_id || '').trim() ? { summaryArtifactId: String(canonicalNode?.summary_artifact_id || '').trim() } : {}),
            ...(Number(canonicalNode?.code_lines || 0) > 0 ? { codeLines: Number(canonicalNode?.code_lines) } : {}),
            ...(Number(canonicalNode?.code_kb || 0) > 0 ? { codeKb: Number(canonicalNode?.code_kb) } : {}),
            ...(Array.isArray(canonicalNode?.code_languages) && canonicalNode?.code_languages.length > 0
                ? { codeLanguages: canonicalNode?.code_languages }
                : {}),
            ...(Number(canonicalNode?.model_latency_ms || 0) > 0 ? { modelLatencyMs: Number(canonicalNode?.model_latency_ms) } : {}),
            log: normalizeNodeLog(
                Array.isArray(rawData.log) && rawData.log.length > 0
                    ? rawData.log
                    : canonicalNode?.activity_log,
            ),
        };
    }, [runCtx.nodeExecutions]);

    const selectedNodeData = useMemo(() => {
        if (!nodeDetailOpen || !selectedNodeSnapshot) return null;
        const currentNodeExecutionId = String(selectedNodeSnapshot.nodeExecutionId || '').trim();
        const currentRawNodeKey = String(selectedNodeSnapshot.rawNodeKey || selectedNodeSnapshot.nodeType || '').trim().toLowerCase();
        const liveNode = workflowNodes.find((node) => {
            const nodeData = node.data as Record<string, unknown>;
            const liveNodeExecutionId = String(nodeData.nodeExecutionId || '').trim();
            const liveRawNodeKey = String(nodeData.rawNodeKey || nodeData.nodeType || '').trim().toLowerCase();
            if (currentNodeExecutionId && liveNodeExecutionId === currentNodeExecutionId) return true;
            return currentRawNodeKey && liveRawNodeKey === currentRawNodeKey;
        });
        return liveNode
            ? buildNodeDetailSnapshot(liveNode.data as Record<string, unknown>)
            : selectedNodeSnapshot;
    }, [buildNodeDetailSnapshot, nodeDetailOpen, selectedNodeSnapshot, workflowNodes]);

    const handleThemeToggle = () => setTheme(current => current === 'dark' ? 'light' : 'dark');
    const connectorRuntimeMode = normalizeRuntimeModeForDisplay(activeRun?.runtime || 'local');
    const connectorRunStatus = activeRun?.status || (runtime.running ? 'running' : 'idle');
    const handleRevealInFinder = useCallback(async (previewUrl: string) => {
        if (!previewUrl || typeof window === 'undefined') return;
        const desktopApi = (window as Window & {
            evermind?: { revealInFinder?: (targetPath: string) => Promise<boolean> | boolean };
        }).evermind;
        if (!desktopApi?.revealInFinder) return;
        try {
            const apiBase = wsUrl.replace('ws://', 'http://').replace('wss://', 'https://').replace(/\/ws$/, '');
            const previewListResp = await fetch(`${apiBase}/api/preview/list`, { cache: 'no-store' });
            if (!previewListResp.ok) return;
            const previewData = await previewListResp.json() as { output_dir?: string };
            const outputDir = String(previewData.output_dir || '/tmp/evermind_output');
            const resolved = new URL(previewUrl, apiBase);
            const previewRelativePath = decodeURIComponent((resolved.pathname.split('/preview/', 2)[1] || '').replace(/^\/+/, ''));
            const revealPath = previewRelativePath ? `${outputDir}/${previewRelativePath}` : outputDir;
            await desktopApi.revealInFinder(revealPath);
        } catch {
            /* noop */
        }
    }, [wsUrl]);

    const previewTaskTitle = useMemo(() => {
        const latestUserGoal = [...chat.messages]
            .reverse()
            .find((message) => message.role === 'user' && message.content.trim());
        return latestUserGoal?.content.trim().slice(0, 120);
    }, [chat.messages]);
    const summaryTaskTitle = summaryTask?.title || (runtime.running ? previewTaskTitle || null : null);

    const defaultEdgeOptions = useMemo(() => ({
        animated: true,
        style: { stroke: 'var(--edge-color)', strokeWidth: 2 },
    }), []);

    return (
        <div className="flex h-screen relative overflow-hidden">
            {/* v7.3: per-task workspace banner — shown when the selected task
                has zero files in its isolated workspace and hasn't been
                dismissed in this session. Sits above everything as a slim
                strip, doesn't push layout. */}
            {workspaceBanner && workspaceBanner.fileCount === 0 && !workspaceBanner.dismissed && (
                <div
                    style={{
                        position: 'fixed', top: 0, left: 0, right: 0, zIndex: 60,
                        display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12,
                        padding: '8px 16px',
                        background: 'linear-gradient(180deg, rgba(111,138,255,0.12) 0%, rgba(111,138,255,0.06) 100%)',
                        borderBottom: '1px solid rgba(111,138,255,0.20)',
                        backdropFilter: 'blur(8px)',
                        fontSize: 12,
                        color: '#d4dcef',
                        animation: 'wsBannerSlideIn 0.3s ease-out',
                    }}
                >
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                        <path d="M2 4.5C2 3.67 2.67 3 3.5 3h3.1l1.6 2H12.5C13.33 5 14 5.67 14 6.5v5C14 12.33 13.33 13 12.5 13h-9C2.67 13 2 12.33 2 11.5v-7Z" stroke="currentColor" strokeWidth="1.4" />
                    </svg>
                    <span>
                        {lang === 'zh' ? '此会话工作区为空。' : "This session's workspace is empty."}
                        <span style={{ color: '#7d8aa3', marginLeft: 6 }}>
                            {lang === 'zh'
                                ? '不同会话的文件互相独立，是否要添加输入文件？'
                                : 'Each session has an isolated folder — add input files?'}
                        </span>
                    </span>
                    <button
                        onClick={triggerAddFiles}
                        style={{
                            padding: '4px 14px', borderRadius: 6, fontSize: 12,
                            background: '#6f8aff', color: '#fff', border: 'none',
                            cursor: 'pointer', fontWeight: 500,
                        }}
                    >
                        {lang === 'zh' ? '＋ 添加文件' : '+ Add Files'}
                    </button>
                    <button
                        onClick={dismissWorkspaceBanner}
                        title={lang === 'zh' ? '本次跳过' : 'Skip for now'}
                        style={{
                            padding: '4px 8px', borderRadius: 6, fontSize: 12,
                            background: 'transparent', color: '#7d8aa3', border: '1px solid rgba(255,255,255,0.08)',
                            cursor: 'pointer',
                        }}
                    >
                        {lang === 'zh' ? '跳过' : 'Skip'}
                    </button>
                    <style jsx>{`@keyframes wsBannerSlideIn { from { transform: translateY(-100%); opacity: 0; } to { transform: translateY(0); opacity: 1; } }`}</style>
                </div>
            )}
            <Sidebar
                onDragStart={workflow.handleSidebarDragStart}
                connected={runtime.connected}
                lang={lang}
                onOpenArtifacts={() => setArtifactsOpen(true)}
                onOpenReports={() => setReportsOpen(true)}
                onOpenSkillsLibrary={() => setSkillsLibraryOpen(true)}
                onOpenFile={(path: string, root: string, content: string, ext: string) => {
                    handleOpenFileInEditor(path, root, content, ext);
                }}
                forcedMode={runtime.canvasView === 'files' ? 'files' : undefined}
            />

            <div className="flex-1 flex flex-col min-w-0">
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
                    onLangToggle={() => {
                        const next: 'en' | 'zh' = lang === 'en' ? 'zh' : 'en';
                        setLang(next);
                        // v5.8.6: auto-sync UI language to backend so node reports
                        // match the user's current language WITHOUT requiring an
                        // explicit "Save to Backend" click. Previously the lang
                        // toggle only changed UI, leaving `ai_bridge.config.ui_language`
                        // stale → reports in Chinese when UI was English and vice versa.
                        try {
                            const ws = runtime.wsRef?.current;
                            if (ws && ws.readyState === WebSocket.OPEN) {
                                ws.send(JSON.stringify({
                                    type: 'update_config',
                                    config: { ui_language: next },
                                }));
                            }
                        } catch { /* non-fatal: user can still save via settings */ }
                    }}
                    theme={theme}
                    onThemeToggle={handleThemeToggle}
                    onOpenSettings={() => setSettingsOpen(true)}
                    onOpenGitHub={() => setGithubOpen(true)}
                    onOpenTemplates={() => setTemplatesOpen(true)}
                    onOpenSkillsLibrary={() => setSkillsLibraryOpen(true)}
                onOpenLessons={() => setLessonsOpen(true)}
                    onOpenGuide={() => setGuideOpen(true)}
                    onOpenHistory={() => setHistoryOpen(true)}
                    onOpenDiagnostics={() => setDiagnosticsOpen(true)}
                    canvasView={runtime.canvasView}
                    onToggleCanvasView={() => runtime.setCanvasView(v => v === 'editor' ? 'preview' : 'editor')}
                    onSetCanvasView={(v) => runtime.setCanvasView(v)}
                    hasPreview={!!runtime.previewUrl}
                    activeRunStatus={connectorRunStatus}
                    runtimeModeLabel={connectorRuntimeMode}
                    activeTaskLabel={summaryTaskTitle || undefined}
                    activeRunId={activeRun?.id}
                    lastEventAt={runtime.connectorLastEventAt}
                    wsUrl={wsUrl}
                    envTag={envTag}
                    onOpenConnectorPanel={OPENCLAW_UI_ENABLED ? () => setConnectorPanelOpen(true) : undefined}
                    showOpenClaw={OPENCLAW_UI_ENABLED}
                />

                <div className="flex flex-1 overflow-hidden min-w-0">
                    <div style={{ flex: 1, minWidth: 0, position: 'relative', overflow: 'hidden', display: 'flex', flexDirection: 'column' }} onDragOver={workflow.onDragOver} onDrop={workflow.onDrop}>
                        {/* §2.2: Task Summary Bar */}
                        {summaryTaskTitle && (
                            <div style={{
                                display: 'flex', alignItems: 'center', gap: 8,
                                padding: '6px 14px', fontSize: 11,
                                borderBottom: '1px solid var(--glass-border)',
                                background: 'rgba(255,255,255,0.02)',
                                color: 'var(--text2)', flexShrink: 0,
                            }}>
                                <span style={{ fontWeight: 700, color: 'var(--text1)', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                    {summaryTaskTitle}
                                </span>
                                <span style={{ color: 'var(--text4)' }}>·</span>
                                {connectorRuntimeMode === 'openclaw' ? (
                                    <span style={{ color: '#c084fc', fontWeight: 600, fontSize: 10 }}>☁ Direct Mode</span>
                                ) : (
                                    <span style={{ color: 'var(--text3)', fontSize: 10 }}>Local</span>
                                )}
                                <span style={{ color: 'var(--text4)' }}>·</span>
                                <span>
                                    {lang === 'zh' ? '节点' : 'Nodes'}: <span style={{ fontWeight: 600, color: summaryCompletedNodes === summaryTotalNodes && summaryTotalNodes > 0 ? '#22c55e' : 'var(--text2)' }}>
                                        {summaryCompletedNodes}/{summaryTotalNodes}
                                    </span>
                                </span>
                                <span style={{ color: 'var(--text4)' }}>·</span>
                                {connectorRunStatus === 'done' && runtime.previewUrl ? (
                                    <span
                                        style={{ fontWeight: 700, color: '#22c55e', cursor: 'pointer', textDecoration: 'underline', textDecorationStyle: 'dotted' }}
                                        onClick={() => runtime.setCanvasView('preview')}
                                        title={lang === 'zh' ? '点击查看交付结果' : 'Click to view deliverable'}
                                    >
                                        {lang === 'zh' ? '📦 交付就绪' : '📦 Deliverable Ready'}
                                    </span>
                                ) : (
                                    <span style={{
                                        fontWeight: 600,
                                        color: connectorRunStatus === 'done' ? '#22c55e'
                                            : connectorRunStatus === 'failed' ? '#ef4444'
                                            : connectorRunStatus === 'running' ? '#3b82f6'
                                            : 'var(--text3)',
                                    }}>
                                        {connectorRunStatus === 'done' ? (lang === 'zh' ? '✅ 流程完成' : '✅ Workflow Done')
                                            : connectorRunStatus === 'running' ? (lang === 'zh' ? '⚡ 执行中' : '⚡ Running')
                                            : connectorRunStatus === 'failed' ? (lang === 'zh' ? '❌ 失败' : '❌ Failed')
                                            : (lang === 'zh' ? '空闲' : 'Idle')}
                                    </span>
                                )}
                                {summaryActiveNodeLabels.length > 0 && (
                                    <>
                                        <span style={{ color: 'var(--text4)' }}>·</span>
                                        <span style={{ color: '#3b82f6', fontSize: 10 }}>
                                            {summaryActiveNodeLabels.join(', ')}
                                        </span>
                                    </>
                                )}
                            </div>
                        )}
                        <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
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
                                onNodeClick={(_event, node) => {
                                    const rawData = node.data as Record<string, unknown>;
                                    setSelectedNodeSnapshot(buildNodeDetailSnapshot(rawData));
                                    setNodeDetailOpen(true);
                                }}
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
                        ) : runtime.canvasView === 'files' ? (
                            <CodeEditorPanel
                                openFiles={openFiles}
                                activeFileIndex={activeFileIndex}
                                onSwitchFile={handleSwitchFile}
                                onCloseFile={handleCloseFileInEditor}
                                onSaveFile={handleSaveFile}
                                onUpdateFileContent={handleUpdateFileContent}
                                lang={lang}
                            />
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
                    </div>
                    {/* Right panel resize handle — 8px hit zone so users can grab it reliably.
                        The inner 2px indicator brightens on hover/drag; outside of those states
                        the visible line is the neighbouring panel border. */}
                    <div
                        onMouseDown={handleResizeStart}
                        style={{
                            width: 8, flexShrink: 0, cursor: 'col-resize',
                            position: 'relative', zIndex: 50,
                            background: 'transparent',
                        }}
                        title="拖动调整宽度 / drag to resize"
                        onMouseEnter={(e) => { if (!resizing) (e.currentTarget.querySelector('[data-indicator]') as HTMLDivElement).style.background = 'rgba(91,140,255,0.55)'; }}
                        onMouseLeave={(e) => { if (!resizing) (e.currentTarget.querySelector('[data-indicator]') as HTMLDivElement).style.background = 'transparent'; }}
                    >
                        <div data-indicator style={{
                            position: 'absolute', top: 0, bottom: 0, left: 3, width: 2,
                            background: resizing ? 'var(--blue)' : 'transparent',
                            transition: resizing ? 'none' : 'background 0.12s',
                        }} />
                    </div>
                    {/* Right panel: Pipeline / Chat mode switcher */}
                    <div style={{ display: 'flex', flexDirection: 'column', flexShrink: 0, overflow: 'hidden', width: rightPanelWidth }}>
                        {/* Tab bar */}
                        <div style={{ display: 'flex', flexShrink: 0, borderLeft: '1px solid var(--glass-border)', borderBottom: '1px solid var(--glass-border)', background: 'rgba(255,255,255,0.02)' }}>
                            <button
                                onClick={() => setChatMode('pipeline')}
                                style={{
                                    flex: 1, padding: '7px 0', fontSize: 11, fontWeight: chatMode === 'pipeline' ? 700 : 400,
                                    color: chatMode === 'pipeline' ? '#3b82f6' : 'var(--text3)',
                                    background: 'transparent', border: 'none', cursor: 'pointer',
                                    borderBottom: chatMode === 'pipeline' ? '2px solid #3b82f6' : '2px solid transparent',
                                }}
                            >
                                Pipeline
                            </button>
                            <button
                                onClick={() => setChatMode('direct')}
                                style={{
                                    flex: 1, padding: '7px 0', fontSize: 11, fontWeight: chatMode === 'direct' ? 700 : 400,
                                    color: chatMode === 'direct' ? '#3b82f6' : 'var(--text3)',
                                    background: 'transparent', border: 'none', cursor: 'pointer',
                                    borderBottom: chatMode === 'direct' ? '2px solid #3b82f6' : '2px solid transparent',
                                }}
                            >
                                Chat
                            </button>
                        </div>
                        {/* Panel content */}
                        <div style={{ flex: 1, overflow: 'hidden' }}>
                            {chatMode === 'pipeline' ? (
                                <ChatPanel
                                    messages={chat.messages}
                                    onSendGoal={runtime.handleSendGoal}
                                    sessionId={chat.activeSessionId}
                                    connected={runtime.connected}
                                    running={runtime.running}
                                    onStop={runtime.handleStop}
                                    lang={lang}
                                    difficulty={difficulty}
                                    onDifficultyChange={setDifficulty}
                                    cliEnabled={cliEnabled}
                                    customCanvasNodeCount={workflowNodes.length}
                                    runtimeMode={connectorRuntimeMode}
                                    showOpenClawRuntime={OPENCLAW_UI_ENABLED}
                                    taskTitle={summaryTaskTitle}
                                    taskStatus={connectorRunStatus}
                                    activeNodeLabels={summaryActiveNodeLabels}
                                    completedNodes={summaryCompletedNodes}
                                    runningNodes={summaryRunningNodes}
                                    totalNodes={summaryTotalNodes}
                                    startedAt={activeRun?.started_at || activeRun?.created_at || null}
                                    onOpenReports={() => setReportsOpen(true)}
                                    onRevealInFinder={handleRevealInFinder}
                                    selectedRuntime={effectiveSelectedRuntime}
                                    onRuntimeChange={OPENCLAW_UI_ENABLED ? setSelectedRuntime : undefined}
                                />
                            ) : (
                                <DirectChatPanel
                                    wsRef={runtime.wsRef}
                                    connected={runtime.connected}
                                    lang={lang}
                                    sessionId={chat.activeSessionId}
                                    onOpenFile={(path, root, content, ext) => handleOpenFileInEditor(path, root, content, ext)}
                                    onFileDiffs={(diffs) => {
                                        // Auto-open files with diff in CodeEditorPanel
                                        for (const d of diffs) {
                                            const ext = d.path.split('.').pop() || '';
                                            const name = d.path.split('/').pop() || d.path;
                                            const existing = openFiles.findIndex(f => f.path === d.path);
                                            if (existing >= 0) {
                                                // Update existing file with diff data
                                                setOpenFiles(prev => prev.map((f, i) => i === existing ? { ...f, content: d.new_content || f.content, originalContent: d.original_content } : f));
                                            } else {
                                                setOpenFiles(prev => [...prev, { path: d.path, name, content: d.new_content || '', ext, originalContent: d.original_content }]);
                                            }
                                        }
                                        setActiveFileIndex(openFiles.length > 0 ? openFiles.length - 1 : 0);
                                        runtime.setCanvasView('files');
                                    }}
                                />
                            )}
                        </div>
                    </div>
                </div>
            </div>

            {/* Modals */}
            {wizardOpen && (
                <WelcomeWizard
                    lang={lang}
                    onPickTemplate={(tpl) => {
                        // Load the template's node graph into the canvas
                        try { workflow.handleLoadTemplate(tpl); } catch { /* ignore */ }
                        // Pre-fill the chat input with the template's goal (if any)
                        if (tpl.goal) {
                            try {
                                window.dispatchEvent(new CustomEvent('evermind-prefill-goal', { detail: { goal: tpl.goal } }));
                            } catch { /* ignore */ }
                        }
                    }}
                    onConfigureKey={() => setSettingsOpen(true)}
                    onSkip={() => { markWelcomeWizardSeen(); setWizardOpen(false); }}
                />
            )}
            <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} lang={lang} onLangChange={setLang} theme={theme} onThemeChange={setTheme} connected={runtime.connected} wsUrl={wsUrl} onWsUrlChange={setWsUrl} wsRef={runtime.wsRef} />
            {githubOpen && <GitHubModal onClose={() => setGithubOpen(false)} lang={lang} />}
            <TemplateGallery
                open={templatesOpen}
                onClose={() => setTemplatesOpen(false)}
                onLoadTemplate={workflow.handleLoadTemplate}
                lang={lang}
                currentCanvas={{
                    nodes: (workflow.nodes || []).map((n: any) => ({
                        id: String(n.id ?? ''),
                        type: String(n.type ?? n.data?.type ?? 'agent'),
                        x: Number(n.x ?? n.position?.x ?? 0),
                        y: Number(n.y ?? n.position?.y ?? 0),
                        data: n.data || { label: n.label, task: n.task },
                    })),
                    // v7.3.9 audit-fix CRITICAL — only use ReactFlow's
                    // canonical {source, target} edge shape. Earlier code
                    // also accepted {from, to} which is undefined for
                    // ReactFlow edges; that caused `findIndex(n => n.id ===
                    // undefined)` to silently return 0 if any node had a
                    // missing id, producing phantom edges in saved templates.
                    edges: (workflow.edges || []).flatMap((e: any) => {
                        const src = e?.source ?? e?.from;
                        const dst = e?.target ?? e?.to;
                        if (!src || !dst) return [];
                        const fromIdx = (workflow.nodes || []).findIndex((n: any) => n.id === src);
                        const toIdx = (workflow.nodes || []).findIndex((n: any) => n.id === dst);
                        if (fromIdx < 0 || toIdx < 0) return [];
                        return [[fromIdx, toIdx] as [number, number]];
                    }),
                }}
            />
            <SkillsLibraryModal open={skillsLibraryOpen} onClose={() => setSkillsLibraryOpen(false)} lang={lang} />
            <LessonsModal open={lessonsOpen} onClose={() => setLessonsOpen(false)} lang={lang === 'en' ? 'en' : 'zh'} />
            <GuideModal
                open={guideOpen}
                onClose={() => setGuideOpen(false)}
                lang={lang}
                onShowWelcomeWizard={() => setWizardOpen(true)}
            />
            <HistoryModal
                open={historyOpen} onClose={() => setHistoryOpen(false)} lang={lang}
                sessions={chat.historySessions} activeSessionId={chat.activeSessionId}
                onSelectSession={(id) => { runtime.handleStop(); chat.handleSelectSession(id); workflow.handleClear(); runtime.setPreviewUrl(null); runtime.setCanvasView('editor'); setHistoryOpen(false); }}
                onCreateSession={() => { runtime.handleStop(); chat.handleCreateSession(); workflow.handleClear(); runtime.setPreviewUrl(null); runtime.setCanvasView('editor'); setHistoryOpen(false); }}
                onDeleteSession={(id) => { chat.handleDeleteSession(id); workflow.handleClear(); runtime.setPreviewUrl(null); runtime.setCanvasView('editor'); }}
                onRenameSession={chat.handleRenameSession}
            />
            <DiagnosticsModal open={diagnosticsOpen} onClose={() => setDiagnosticsOpen(false)} lang={lang} />
            <ArtifactsModal open={artifactsOpen} onClose={() => setArtifactsOpen(false)} lang={lang} />
            <ReportsModal open={reportsOpen} onClose={() => setReportsOpen(false)} lang={lang} reports={reports.runReports} onDeleteReport={reports.deleteReport} onClearReports={reports.clearReports} />
            <NodeDetailPopup
                open={nodeDetailOpen}
                onClose={() => { setNodeDetailOpen(false); setSelectedNodeSnapshot(null); }}
                lang={lang}
                nodeData={selectedNodeData as Parameters<typeof NodeDetailPopup>[0]['nodeData']}
            />
            {OPENCLAW_UI_ENABLED && (
                <OpenClawPanel
                    open={connectorPanelOpen}
                    onClose={() => setConnectorPanelOpen(false)}
                    connected={runtime.connected}
                    running={runtime.running}
                    lang={lang}
                    wsUrl={wsUrl}
                    events={connectorEvents}
                    runtimeMode={connectorRuntimeMode}
                    activeRunStatus={connectorRunStatus}
                    activeRunId={activeRun?.id}
                    runtimeId={runtime.connectorRuntimeId}
                    processId={runtime.connectorPid}
                    connectedAt={runtime.connectorConnectedAt}
                    lastEventAt={runtime.connectorLastEventAt}
                    onReconnect={runtime.reconnect}
                />
            )}

            {/* ── File Viewer Overlay (with Monaco syntax highlighting) ── */}
            {openedFile && (() => {
                const overlayExt = openedFile.ext.toLowerCase().replace(/^\./, '');
                const OVERLAY_EXT_LANG: Record<string, string> = {
                    js: 'javascript', jsx: 'javascript', mjs: 'javascript',
                    ts: 'typescript', tsx: 'typescript',
                    py: 'python', html: 'html', htm: 'html', xml: 'xml', svg: 'xml',
                    css: 'css', scss: 'scss', less: 'less',
                    json: 'json', md: 'markdown',
                    sh: 'shell', bash: 'shell', zsh: 'shell',
                    yaml: 'yaml', yml: 'yaml', sql: 'sql',
                    go: 'go', rs: 'rust', java: 'java',
                    c: 'c', cpp: 'cpp', h: 'c', hpp: 'cpp',
                    lua: 'lua', rb: 'ruby', php: 'php', swift: 'swift',
                    kt: 'kotlin', dart: 'dart', r: 'r',
                    dockerfile: 'dockerfile', graphql: 'graphql',
                    toml: 'ini', ini: 'ini', env: 'ini',
                };
                const overlayLang = OVERLAY_EXT_LANG[overlayExt] || 'plaintext';
                const isImage = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(overlayExt);
                const isHtml = ['html', 'htm'].includes(overlayExt);
                const isCode = !isImage && !isHtml;

                return (
                <div style={{
                    position: 'fixed', inset: 0, zIndex: 9999,
                    background: 'rgba(0,0,0,0.75)', backdropFilter: 'blur(10px)',
                    display: 'flex', flexDirection: 'column',
                }}>
                    {/* Header — Evermind dark glassmorphic */}
                    <div style={{
                        display: 'flex', alignItems: 'center', gap: 10,
                        padding: '10px 16px',
                        background: 'linear-gradient(180deg, rgba(13,17,23,0.98), rgba(22,27,34,0.96))',
                        borderBottom: '1px solid rgba(91,140,255,0.14)',
                        flexShrink: 0,
                    }}>
                        {/* Breadcrumb-style path */}
                        <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 4, overflow: 'hidden' }}>
                            {openedFile.path.replace(/^\//, '').split('/').map((part, i, arr) => (
                                <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: i < arr.length - 1 ? 1 : 0, minWidth: 0 }}>
                                    {i > 0 && <span style={{ color: '#484f58', fontSize: 10 }}>/</span>}
                                    <span style={{
                                        fontSize: i === arr.length - 1 ? 13 : 11,
                                        fontWeight: i === arr.length - 1 ? 700 : 400,
                                        color: i === arr.length - 1 ? '#e6edf3' : '#8b949e',
                                        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                                    }}>{part}</span>
                                </span>
                            ))}
                        </div>
                        <span style={{
                            fontSize: 9, color: '#58a6ff', textTransform: 'uppercase', fontWeight: 800,
                            letterSpacing: '0.06em',
                            padding: '2px 8px', borderRadius: 4,
                            background: 'rgba(88,166,255,0.12)', border: '1px solid rgba(88,166,255,0.2)',
                        }}>
                            {overlayExt || '?'}
                        </span>
                        {isCode && (
                            <button
                                onClick={() => {
                                    handleOpenFileInEditor(openedFile.path, openedFile.root, openedFile.content, openedFile.ext);
                                    setOpenedFile(null);
                                }}
                                style={{
                                    background: 'rgba(88,166,255,0.1)', border: '1px solid rgba(88,166,255,0.25)',
                                    borderRadius: 6, padding: '4px 12px', cursor: 'pointer',
                                    color: '#58a6ff', fontSize: 10, fontWeight: 600,
                                }}
                            >
                                {lang === 'zh' ? '在编辑器打开' : 'Open in Editor'}
                            </button>
                        )}
                        <button
                            onClick={() => setOpenedFile(null)}
                            style={{
                                background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)',
                                borderRadius: 6, padding: '4px 12px', cursor: 'pointer',
                                color: '#e6edf3', fontSize: 11, fontWeight: 600,
                                transition: 'all 0.15s',
                            }}
                            onMouseEnter={e => { e.currentTarget.style.background = 'rgba(255,255,255,0.12)'; }}
                            onMouseLeave={e => { e.currentTarget.style.background = 'rgba(255,255,255,0.06)'; }}
                        >
                            {lang === 'zh' ? '关闭' : 'Close'} ✕
                        </button>
                    </div>
                    {/* Content */}
                    <div style={{ flex: 1, overflow: 'hidden', background: '#0d1117' }}>
                        {isHtml ? (
                            <iframe
                                srcDoc={openedFile.content}
                                style={{ width: '100%', height: '100%', border: 'none', background: '#fff' }}
                                sandbox="allow-scripts allow-same-origin allow-pointer-lock"
                                title={openedFile.path}
                            />
                        ) : isImage ? (
                            openedFile.ext === '.svg' ? (
                                <div style={{ padding: 32, display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%' }}>
                                    <div dangerouslySetInnerHTML={{ __html: openedFile.content }} style={{ maxWidth: '80%' }} />
                                </div>
                            ) : (
                                <div style={{ padding: 32, display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%' }}>
                                    {/* eslint-disable-next-line @next/next/no-img-element */}
                                    <img
                                        src={`data:image/${overlayExt};base64,${openedFile.content}`}
                                        alt={openedFile.path}
                                        style={{ maxWidth: '90%', maxHeight: '80vh', objectFit: 'contain', borderRadius: 8 }}
                                    />
                                </div>
                            )
                        ) : (
                            <MonacoEditor
                                language={overlayLang}
                                value={openedFile.content}
                                theme="evermind-dark"
                                beforeMount={(monaco) => {
                                    // Register evermind-dark if not already registered
                                    try {
                                        monaco.editor.defineTheme('evermind-dark', {
                                            base: 'vs-dark',
                                            inherit: true,
                                            rules: [
                                                { token: 'comment', foreground: '6a9955', fontStyle: 'italic' },
                                                { token: 'keyword', foreground: 'c586c0' },
                                                { token: 'keyword.control', foreground: 'c586c0' },
                                                { token: 'storage', foreground: '569cd6' },
                                                { token: 'storage.type', foreground: '569cd6' },
                                                { token: 'string', foreground: 'ce9178' },
                                                { token: 'number', foreground: 'b5cea8' },
                                                { token: 'entity.name.function', foreground: 'dcdcaa' },
                                                { token: 'support.function', foreground: 'dcdcaa' },
                                                { token: 'variable', foreground: '9cdcfe' },
                                                { token: 'type', foreground: '4ec9b0' },
                                                { token: 'tag', foreground: '569cd6' },
                                                { token: 'attribute.name', foreground: '9cdcfe' },
                                                { token: 'attribute.value', foreground: 'ce9178' },
                                                { token: 'constant', foreground: '4fc1ff' },
                                                { token: 'delimiter.bracket', foreground: 'ffd700' },
                                                { token: 'regexp', foreground: 'd16969' },
                                                { token: 'annotation', foreground: 'dcdcaa' },
                                            ],
                                            colors: {
                                                'editor.background': '#0d1117',
                                                'editor.foreground': '#e6edf3',
                                                'editor.selectionBackground': '#264f78',
                                                'editor.lineHighlightBackground': '#161b2280',
                                                'editorLineNumber.foreground': '#484f58',
                                                'editorLineNumber.activeForeground': '#8b949e',
                                                'editorGutter.background': '#0d1117',
                                                'editorCursor.foreground': '#58a6ff',
                                                'editorBracketMatch.background': '#3b82f633',
                                                'editorBracketMatch.border': '#3b82f699',
                                                'editorIndentGuide.background': '#21262d',
                                                'scrollbar.shadow': '#00000000',
                                                'scrollbarSlider.background': '#484f5833',
                                                'minimap.background': '#0d1117',
                                                'editorOverviewRuler.border': '#0d1117',
                                                'editorWidget.background': '#161b22',
                                                'editorWidget.border': '#30363d',
                                            },
                                        });
                                    } catch { /* theme may already be defined */ }
                                }}
                                options={{
                                    readOnly: true,
                                    fontSize: 13,
                                    fontFamily: "'JetBrains Mono','Fira Code','SF Mono','Cascadia Code',Menlo,monospace",
                                    fontLigatures: true,
                                    lineHeight: 20,
                                    minimap: { enabled: true, scale: 1, showSlider: 'mouseover' },
                                    scrollBeyondLastLine: false,
                                    renderWhitespace: 'selection',
                                    bracketPairColorization: { enabled: true },
                                    guides: { bracketPairs: true, indentation: true },
                                    smoothScrolling: true,
                                    padding: { top: 8, bottom: 8 },
                                    automaticLayout: true,
                                    tabSize: 2,
                                    wordWrap: 'off',
                                    folding: true,
                                    lineNumbers: 'on',
                                    renderLineHighlight: 'line',
                                    overviewRulerBorder: false,
                                    scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10, useShadows: false },
                                    domReadOnly: true,
                                }}
                                loading={
                                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#8b949e', background: '#0d1117' }}>
                                        <span style={{ fontSize: 12 }}>{lang === 'zh' ? '加载中...' : 'Loading...'}</span>
                                    </div>
                                }
                            />
                        )}
                    </div>
                </div>
                );
            })()}
        </div>
    );
}
