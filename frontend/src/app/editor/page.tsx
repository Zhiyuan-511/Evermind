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
    const [difficulty, setDifficulty] = useState<'simple' | 'standard' | 'pro'>('standard');
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

    // If the current selection is stale or inactive after reconnect, refocus the most active task.
    useEffect(() => {
        if (!runtimeConnected) return;
        if (!preferredTaskForHydration?.id) return;
        if (selectedTask?.id === preferredTaskForHydration.id) return;
        if (!selectedTask || !isHydratableTaskStatus(selectedTask.status)) {
            selectTask(preferredTaskForHydration.id);
        }
    }, [preferredTaskForHydration?.id, runtimeConnected, selectTask, selectedTask, selectedTask?.id, selectedTask?.status]);

    // Once runs are available, auto-select the active run when the current selection is stale.
    useEffect(() => {
        if (!runtimeConnected) return;
        if (!preferredRunForHydration?.id) return;
        if (selectedRun?.id === preferredRunForHydration.id) return;
        const selectedRunMatchesTask = selectedRun?.task_id === selectedTask?.id;
        if (!selectedRun || !isActiveRunStatus(selectedRun.status) || !selectedRunMatchesTask) {
            selectRun(preferredRunForHydration.id);
        }
    }, [preferredRunForHydration?.id, runtimeConnected, selectRun, selectedRun, selectedRun?.id, selectedRun?.status, selectedRun?.task_id, selectedTask?.id]);

    // Re-pull node executions for the selected run after reconnect so the timeline/canvas catch up immediately.
    useEffect(() => {
        if (!runtimeConnected) return;
        if (!selectedRun?.id) return;
        void fetchNodeExecutions(selectedRun.id);
    }, [fetchNodeExecutions, runtimeConnected, selectedRun?.id]);

    // §3.1: Rebuild canvas from existing NEs ONLY when reconnecting or restoring state.
    // Skip when canvas already has agent nodes (plan_created already built them).
    // This prevents hydration from overwriting live status updates (e.g., subtask_start → running).
    const hydrationDoneRef = useRef<string>(''); // Track which run we already hydrated
    useEffect(() => {
        if (!selectedRun?.id) return;
        const runId = selectedRun.id;
        const runStatus = String(selectedRun.status || '').trim().toLowerCase();
        if (!HYDRATION_ACTIVE_STATUSES.has(runStatus)) return; // Skip terminal runs

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
                    ...(Number.isFinite(Number(ne.progress)) ? { progress: Number(ne.progress) } : (isWarmupLeadNode ? { progress: 5 } : {})),
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
    const [templatesOpen, setTemplatesOpen] = useState(false);
    const [skillsLibraryOpen, setSkillsLibraryOpen] = useState(false);
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
            <Sidebar
                onDragStart={workflow.handleSidebarDragStart}
                connected={runtime.connected}
                lang={lang}
                onOpenArtifacts={() => setArtifactsOpen(true)}
                onOpenReports={() => setReportsOpen(true)}
                onOpenSkillsLibrary={() => setSkillsLibraryOpen(true)}
                onOpenFile={handleOpenFile}
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
                        sessionId={chat.activeSessionId}
                        connected={runtime.connected}
                        running={runtime.running}
                        onStop={runtime.handleStop}
                        lang={lang}
                        difficulty={difficulty}
                        onDifficultyChange={setDifficulty}
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

            {/* ── File Viewer Overlay ── */}
            {openedFile && (
                <div style={{
                    position: 'fixed', inset: 0, zIndex: 9999,
                    background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(6px)',
                    display: 'flex', flexDirection: 'column',
                }}>
                    {/* Header */}
                    <div style={{
                        display: 'flex', alignItems: 'center', gap: 10,
                        padding: '10px 16px',
                        background: 'rgba(15,17,23,0.95)',
                        borderBottom: '1px solid rgba(255,255,255,0.08)',
                        flexShrink: 0,
                    }}>
                        <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text1)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {openedFile.path}
                        </span>
                        <span style={{ fontSize: 10, color: 'var(--text4)', textTransform: 'uppercase', fontWeight: 700 }}>
                            {openedFile.ext.replace('.', '')}
                        </span>
                        <button
                            onClick={() => setOpenedFile(null)}
                            style={{
                                background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)',
                                borderRadius: 6, padding: '4px 12px', cursor: 'pointer',
                                color: 'var(--text2)', fontSize: 11, fontWeight: 600,
                            }}
                        >
                            {lang === 'zh' ? '关闭' : 'Close'} ✕
                        </button>
                    </div>
                    {/* Content */}
                    <div style={{ flex: 1, overflow: 'auto', background: 'rgba(15,17,23,0.98)' }}>
                        {['.html', '.htm'].includes(openedFile.ext) ? (
                            <iframe
                                srcDoc={openedFile.content}
                                style={{ width: '100%', height: '100%', border: 'none', background: '#fff' }}
                                sandbox="allow-scripts allow-same-origin allow-pointer-lock"
                                title={openedFile.path}
                            />
                        ) : ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'].includes(openedFile.ext) ? (
                            openedFile.ext === '.svg' ? (
                                <div style={{ padding: 32, display: 'flex', justifyContent: 'center' }}>
                                    <div dangerouslySetInnerHTML={{ __html: openedFile.content }} style={{ maxWidth: '80%' }} />
                                </div>
                            ) : (
                                <div style={{ padding: 32, display: 'flex', justifyContent: 'center' }}>
                                    {/* eslint-disable-next-line @next/next/no-img-element */}
                                    <img
                                        src={`data:image/${openedFile.ext.replace('.', '')};base64,${openedFile.content}`}
                                        alt={openedFile.path}
                                        style={{ maxWidth: '90%', maxHeight: '80vh', objectFit: 'contain', borderRadius: 8 }}
                                    />
                                </div>
                            )
                        ) : (
                            <pre style={{
                                margin: 0, padding: '16px 20px',
                                fontSize: 12, lineHeight: 1.7,
                                fontFamily: 'var(--font-mono), "SF Mono", "Fira Code", monospace',
                                color: 'var(--text1)',
                                whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                                minHeight: '100%',
                            }}>
                                {openedFile.content}
                            </pre>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}
