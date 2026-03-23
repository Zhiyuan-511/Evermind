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
import AgentNode from '@/components/AgentNode';
import SettingsModal from '@/components/SettingsModal';
import TemplateGallery from '@/components/TemplateGallery';
import SkillsLibraryModal from '@/components/SkillsLibraryModal';
import GuideModal from '@/components/GuideModal';
import HistoryModal from '@/components/HistoryModal';
import DiagnosticsModal from '@/components/DiagnosticsModal';
import ArtifactsModal from '@/components/ArtifactsModal';
import ReportsModal from '@/components/ReportsModal';
import NodeDetailPopup from '@/components/NodeDetailPopup';
import PreviewCenter from '@/components/PreviewCenter';
import OpenClawPanel from '@/components/OpenClawPanel';
import type { ConnectorEvent } from '@/components/OpenClawPanel';
import { NODE_TYPES } from '@/lib/types';

import { useChatHistory } from '@/hooks/useChatHistory';
import { useRunReports } from '@/hooks/useRunReports';
import { useWorkflowState } from '@/hooks/useWorkflowState';
import { useRuntimeConnection } from '@/hooks/useRuntimeConnection';
import { TaskRunProvider, useTaskContext, useRunContext } from '@/contexts/TaskRunProvider';

const THEME_STORAGE_KEY = 'evermind-theme';
const RUNTIME_STORAGE_KEY = 'evermind-runtime';

const nodeTypes = { agent: AgentNode };
const ACTIVE_RUN_STATUSES = new Set(['queued', 'running', 'waiting_review', 'waiting_selfcheck', 'done', 'completed', 'failed']);
const HYDRATABLE_TASK_STATUSES = new Set(['executing', 'review', 'selfcheck', 'done', 'completed']);

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
    const [difficulty, setDifficulty] = useState<'simple' | 'standard' | 'pro'>('standard');
    const [selectedRuntime, setSelectedRuntime] = useState<'local' | 'openclaw'>(() => {
        if (typeof window === 'undefined') return 'local';
        try {
            const saved = window.localStorage.getItem(RUNTIME_STORAGE_KEY);
            return saved === 'openclaw' ? 'openclaw' : 'local';
        } catch {
            return 'local';
        }
    });

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
        try { window.localStorage.setItem(RUNTIME_STORAGE_KEY, selectedRuntime); } catch { /* ignore */ }
    }, [selectedRuntime]);

    // ── P0-1: Canonical state from context ──
    const taskCtx = useTaskContext();
    const runCtx = useRunContext();
    const reconnectRunIds = useMemo(
        () => runCtx.runs
            .filter((run) => run.runtime === 'openclaw' && run.status === 'running')
            .map((run) => run.id),
        [runCtx.runs],
    );
    const preferredTaskForHydration = useMemo(() => (
        [...taskCtx.tasks]
            .filter((task) => isHydratableTaskStatus(task.status))
            .sort((a, b) => b.updatedAt - a.updatedAt)[0] || null
    ), [taskCtx.tasks]);
    const preferredRunForHydration = useMemo(() => {
        const activeOpenClawRuns = runCtx.runs
            .filter((run) => run.runtime === 'openclaw' && isActiveRunStatus(run.status))
            .sort((a, b) => b.updated_at - a.updated_at);
        if (activeOpenClawRuns.length > 0) return activeOpenClawRuns[0];

        const activeRuns = runCtx.runs
            .filter((run) => isActiveRunStatus(run.status))
            .sort((a, b) => b.updated_at - a.updated_at);
        return activeRuns[0] || null;
    }, [runCtx.runs]);
    const activeRun = useMemo(() => {
        const activeOpenClawRuns = runCtx.runs
            .filter((run) => run.runtime === 'openclaw' && isActiveRunStatus(run.status))
            .sort((a, b) => b.updated_at - a.updated_at);
        if (activeOpenClawRuns.length > 0) return activeOpenClawRuns[0];

        const activeRuns = runCtx.runs
            .filter((run) => isActiveRunStatus(run.status))
            .sort((a, b) => b.updated_at - a.updated_at);
        if (activeRuns.length > 0) return activeRuns[0];

        if (runCtx.selectedRun) return runCtx.selectedRun;
        return runCtx.latestRun;
    }, [runCtx.latestRun, runCtx.runs, runCtx.selectedRun]);
    const summaryTask = useMemo(() => {
        if (activeRun?.task_id) {
            const linked = taskCtx.tasks.find((task) => task.id === activeRun.task_id);
            if (linked) return linked;
        }
        return preferredTaskForHydration;
    }, [activeRun, preferredTaskForHydration, taskCtx.tasks]);
    const summaryActiveNodeLabels = useMemo(() => {
        if (!activeRun || runCtx.selectedRun?.id !== activeRun.id) return [];
        const activeIds = activeRun.active_node_execution_ids || [];
        const labels = activeIds
            .map((id) => runCtx.nodeExecutions.find((node) => node.id === id))
            .filter(Boolean)
            .map((node) => node!.node_label || node!.node_key)
            .filter(Boolean);
        return [...new Set(labels)];
    }, [activeRun, runCtx.nodeExecutions, runCtx.selectedRun]);
    const TERMINAL_NE_STATUSES = new Set(['passed', 'failed', 'skipped', 'cancelled', 'done', 'completed']);
    const summaryCompletedNodes = useMemo(() => {
        if (!activeRun || runCtx.selectedRun?.id !== activeRun.id) return 0;
        return runCtx.nodeExecutions.filter((node) =>
            node.run_id === activeRun.id && TERMINAL_NE_STATUSES.has(String(node.status || '').trim().toLowerCase())
        ).length;
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeRun, runCtx.nodeExecutions, runCtx.selectedRun]);
    const summaryTotalNodes = activeRun?.node_execution_ids?.length
        || (activeRun ? runCtx.nodeExecutions.filter(ne => ne.run_id === activeRun.id).length : 0);

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
        wsUrl, lang, difficulty, goalRuntime: selectedRuntime, sessionId: chat.activeSessionId,
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
        onConnectorEvent: appendConnectorEvent,
    });

    // §2.4: Auto-fetch tasks when WS connects (hydrate state even if events were missed)
    useEffect(() => {
        if (runtime.connected) {
            void taskCtx.fetchTasks();
        }
    }, [runtime.connected, taskCtx.fetchTasks]);

    // Refresh runs for the selected task when the WS reconnects so missed transitions hydrate immediately.
    useEffect(() => {
        if (!runtime.connected) return;
        if (!taskCtx.selectedTask?.id) return;
        void runCtx.fetchRuns();
    }, [runtime.connected, taskCtx.selectedTask?.id, runCtx.fetchRuns]);

    // If the current selection is stale or inactive after reconnect, refocus the most active task.
    useEffect(() => {
        if (!runtime.connected) return;
        if (!preferredTaskForHydration?.id) return;
        if (taskCtx.selectedTask?.id === preferredTaskForHydration.id) return;
        if (!taskCtx.selectedTask || !isHydratableTaskStatus(taskCtx.selectedTask.status)) {
            taskCtx.selectTask(preferredTaskForHydration.id);
        }
    }, [runtime.connected, preferredTaskForHydration?.id, taskCtx.selectedTask?.id, taskCtx.selectedTask?.status, taskCtx.selectTask]);

    // Once runs are available, auto-select the active run when the current selection is stale.
    useEffect(() => {
        if (!runtime.connected) return;
        if (!preferredRunForHydration?.id) return;
        if (runCtx.selectedRun?.id === preferredRunForHydration.id) return;
        const selectedRunMatchesTask = runCtx.selectedRun?.task_id === taskCtx.selectedTask?.id;
        if (!runCtx.selectedRun || !isActiveRunStatus(runCtx.selectedRun.status) || !selectedRunMatchesTask) {
            runCtx.selectRun(preferredRunForHydration.id);
        }
    }, [runtime.connected, preferredRunForHydration?.id, runCtx.selectedRun?.id, runCtx.selectedRun?.status, runCtx.selectedRun?.task_id, taskCtx.selectedTask?.id, runCtx.selectRun]);

    // Re-pull node executions for the selected run after reconnect so the timeline/canvas catch up immediately.
    useEffect(() => {
        if (!runtime.connected) return;
        if (!runCtx.selectedRun?.id) return;
        void runCtx.fetchNodeExecutions(runCtx.selectedRun.id);
    }, [runtime.connected, runCtx.selectedRun?.id, runCtx.fetchNodeExecutions]);

    // §3.1: Rebuild canvas from existing NEs ONLY when reconnecting or restoring state.
    // Skip when canvas already has agent nodes (plan_created already built them).
    // This prevents hydration from overwriting live status updates (e.g., subtask_start → running).
    const HYDRATION_ACTIVE_STATUSES = new Set(['running', 'queued', 'waiting_review', 'waiting_selfcheck', 'executing']);
    const hydrationDoneRef = useRef<string>(''); // Track which run we already hydrated
    useEffect(() => {
        if (!runCtx.selectedRun?.id) return;
        const runId = runCtx.selectedRun.id;
        const runStatus = String(runCtx.selectedRun.status || '').trim().toLowerCase();
        if (!HYDRATION_ACTIVE_STATUSES.has(runStatus)) return; // Skip terminal runs

        // If we already hydrated this run, don't rebuild — live events handle updates
        if (hydrationDoneRef.current === runId) return;

        const runNEs = runCtx.nodeExecutions.filter(ne => ne.run_id === runCtx.selectedRun?.id);
        if (runNEs.length === 0) return;

        // Debounce slightly to let React state settle after NE fetch
        const timer = setTimeout(() => {
            const existingAgentNodes = workflow.nodes.filter((n) => n.type === 'agent');
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
                nodeMap = workflow.buildPlanNodes(subtasksWithIdDeps, lang);
            }

            for (const ne of runNEs) {
                const neId = String(ne.id || '').trim();
                const nodeKey = String(ne.node_key || '').trim().toLowerCase();
                const normalizedType = normalizeHydrationNodeType(ne.node_key || 'builder');
                const canvasNodeId = nodeMap[neId]
                    || workflow.nodes.find((node) =>
                        String(node.data?.nodeExecutionId || '').trim() === neId
                        || String(node.data?.rawNodeKey || '').trim().toLowerCase() === nodeKey
                        || String(node.data?.nodeType || '').trim().toLowerCase() === normalizedType
                        || String(node.data?.label || '').trim() === String(ne.node_label || '').trim(),
                    )?.id;
                if (!canvasNodeId) continue;
                const startedAt = normalizeEpochMs(ne.started_at, 0);
                const endedAt = normalizeEpochMs(ne.ended_at, 0);
                const durationSeconds = deriveDurationSeconds(startedAt, endedAt);
                workflow.updateNodeData(canvasNodeId, {
                    nodeExecutionId: String(ne.id || ''),
                    rawNodeKey: String(ne.node_key || ''),
                    nodeType: normalizeHydrationNodeType(ne.node_key || 'builder'),
                    label: String(ne.node_label || ne.node_key || ''),
                    status: String(ne.status || 'queued'),
                    runtime: runCtx.selectedRun?.runtime === 'openclaw' ? 'openclaw' : 'local',
                    ...(Number.isFinite(Number(ne.progress)) ? { progress: Number(ne.progress) } : {}),
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
                    ...(String(ne.phase || '').trim() ? { phase: String(ne.phase || '').trim() } : {}),
                });
            }
            hydrationDoneRef.current = runId;
        }, 150);
        return () => clearTimeout(timer);
    }, [lang, runCtx.nodeExecutions, runCtx.selectedRun?.id, runCtx.selectedRun?.runtime, runCtx.selectedRun?.status, workflow.buildPlanNodes, workflow.nodes, workflow.updateNodeData]);

    // §3.5b: Auto-switch to preview once a completed run also has a ready preview.
    const previewAutoSwitchStateRef = useRef<{ runId: string; status: string; hadPreview: boolean }>({
        runId: '',
        status: '',
        hadPreview: false,
    });
    useEffect(() => {
        const runId = String(runCtx.selectedRun?.id || '');
        const runStatus = String(runCtx.selectedRun?.status || '');
        const hasPreview = Boolean(runtime.previewUrl);
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
            runtime.setCanvasView('preview');
        }

        previewAutoSwitchStateRef.current = {
            runId,
            status: runStatus,
            hadPreview: hasPreview,
        };
    }, [runCtx.selectedRun?.id, runCtx.selectedRun?.status, runtime.previewUrl, runtime.setCanvasView]);

    // ── Modal states ──
    const [settingsOpen, setSettingsOpen] = useState(false);
    const [templatesOpen, setTemplatesOpen] = useState(false);
    const [skillsLibraryOpen, setSkillsLibraryOpen] = useState(false);
    const [guideOpen, setGuideOpen] = useState(false);
    const [historyOpen, setHistoryOpen] = useState(false);
    const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
    const [artifactsOpen, setArtifactsOpen] = useState(false);
    const [reportsOpen, setReportsOpen] = useState(false);
    const [nodeDetailOpen, setNodeDetailOpen] = useState(false);
    const [selectedNodeData, setSelectedNodeData] = useState<Record<string, unknown> | null>(null);

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
            log: normalizeNodeLog(
                Array.isArray(rawData.log) && rawData.log.length > 0
                    ? rawData.log
                    : canonicalNode?.activity_log,
            ),
        };
    }, [runCtx.nodeExecutions]);

    useEffect(() => {
        if (!nodeDetailOpen || !selectedNodeData) return;
        const currentNodeExecutionId = String(selectedNodeData.nodeExecutionId || '').trim();
        const currentRawNodeKey = String(selectedNodeData.rawNodeKey || selectedNodeData.nodeType || '').trim().toLowerCase();
        const liveNode = workflow.nodes.find((node) => {
            const nodeData = node.data as Record<string, unknown>;
            const liveNodeExecutionId = String(nodeData.nodeExecutionId || '').trim();
            const liveRawNodeKey = String(nodeData.rawNodeKey || nodeData.nodeType || '').trim().toLowerCase();
            if (currentNodeExecutionId && liveNodeExecutionId === currentNodeExecutionId) return true;
            return currentRawNodeKey && liveRawNodeKey === currentRawNodeKey;
        });
        if (!liveNode) return;
        const nextSnapshot = buildNodeDetailSnapshot(liveNode.data as Record<string, unknown>);
        const prevSerialized = JSON.stringify(selectedNodeData);
        const nextSerialized = JSON.stringify(nextSnapshot);
        if (prevSerialized !== nextSerialized) {
            setSelectedNodeData(nextSnapshot);
        }
    }, [buildNodeDetailSnapshot, nodeDetailOpen, selectedNodeData, workflow.nodes]);

    const handleThemeToggle = () => setTheme(current => current === 'dark' ? 'light' : 'dark');
    const connectorRuntimeMode = activeRun?.runtime || 'local';
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
            <Sidebar
                onDragStart={workflow.handleSidebarDragStart}
                connected={runtime.connected}
                lang={lang}
                onOpenArtifacts={() => setArtifactsOpen(true)}
                onOpenReports={() => setReportsOpen(true)}
                onOpenSkillsLibrary={() => setSkillsLibraryOpen(true)}

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
                    onLangToggle={() => setLang(l => l === 'en' ? 'zh' : 'en')}
                    theme={theme}
                    onThemeToggle={handleThemeToggle}
                    onOpenSettings={() => setSettingsOpen(true)}
                    onOpenTemplates={() => setTemplatesOpen(true)}
                    onOpenSkillsLibrary={() => setSkillsLibraryOpen(true)}
                    onOpenGuide={() => setGuideOpen(true)}
                    onOpenHistory={() => setHistoryOpen(true)}
                    onOpenDiagnostics={() => setDiagnosticsOpen(true)}
                    canvasView={runtime.canvasView}
                    onToggleCanvasView={() => runtime.setCanvasView(v => v === 'editor' ? 'preview' : 'editor')}
                    hasPreview={!!runtime.previewUrl}
                    activeRunStatus={connectorRunStatus}
                    runtimeModeLabel={connectorRuntimeMode}
                    activeTaskLabel={summaryTaskTitle || undefined}
                    activeRunId={activeRun?.id}
                    lastEventAt={runtime.connectorLastEventAt}
                    wsUrl={wsUrl}
                    envTag={envTag}
                    onOpenConnectorPanel={() => setConnectorPanelOpen(true)}
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
                                    setSelectedNodeData(buildNodeDetailSnapshot(rawData));
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
                    <ChatPanel
                        messages={chat.messages}
                        onSendGoal={runtime.handleSendGoal}
                        connected={runtime.connected}
                        running={runtime.running}
                        onStop={runtime.handleStop}
                        lang={lang}
                        difficulty={difficulty}
                        onDifficultyChange={setDifficulty}
                        runtimeMode={connectorRuntimeMode}
                        taskTitle={summaryTaskTitle}
                        taskStatus={connectorRunStatus}
                        activeNodeLabels={summaryActiveNodeLabels}
                        completedNodes={summaryCompletedNodes}
                        totalNodes={summaryTotalNodes}
                        startedAt={activeRun?.started_at || activeRun?.created_at || null}
                        onOpenReports={() => setReportsOpen(true)}
                        onRevealInFinder={handleRevealInFinder}
                        selectedRuntime={selectedRuntime}
                        onRuntimeChange={setSelectedRuntime}
                    />
                </div>
            </div>

            {/* Modals */}
            <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} lang={lang} onLangChange={setLang} theme={theme} onThemeChange={setTheme} connected={runtime.connected} wsUrl={wsUrl} onWsUrlChange={setWsUrl} wsRef={runtime.wsRef} />
            <TemplateGallery open={templatesOpen} onClose={() => setTemplatesOpen(false)} onLoadTemplate={workflow.handleLoadTemplate} lang={lang} />
            <SkillsLibraryModal open={skillsLibraryOpen} onClose={() => setSkillsLibraryOpen(false)} lang={lang} />
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
            <NodeDetailPopup
                open={nodeDetailOpen}
                onClose={() => { setNodeDetailOpen(false); setSelectedNodeData(null); }}
                lang={lang}
                nodeData={selectedNodeData as Parameters<typeof NodeDetailPopup>[0]['nodeData']}
            />
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
        </div>
    );
}
