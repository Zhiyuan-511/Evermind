'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useWebSocket } from '@/hooks/useWebSocket';
import { NODE_TYPES, type ChatMessage, type RunReportRecord, type TaskCard, type RunRecord, type NodeExecutionRecord } from '@/lib/types';
import { buildMessageContentForHistory, defaultGoalFromAttachments } from '@/lib/chatAttachments';
import type { ChatAttachment } from '@/lib/types';
import { buildReadableCurrentWork, describeNodeActivity } from '@/lib/nodeOutputHumanizer';
import { buildRunGoalPlan } from '@/lib/workflowPlan';
import { saveArtifact } from '@/lib/api';
import type { Node, Edge as RFEdge } from '@xyflow/react';

// ── Helpers ──
function escapeHtml(text: string): string {
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
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

function toEpochSeconds(value: unknown, fallback = Date.now() / 1000): number {
    const num = Number(value || 0);
    if (!Number.isFinite(num) || num <= 0) return fallback;
    return num > 10_000_000_000 ? num / 1000 : num;
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

function normalizeCanvasNodeType(value: unknown): string {
    const raw = String(value || '').trim().toLowerCase();
    if (!raw) return 'builder';
    if (NODE_TYPES[raw]) return raw;

    const withoutNumericSuffix = raw.replace(/(?:[_-]?\d+)+$/, '');
    if (withoutNumericSuffix && NODE_TYPES[withoutNumericSuffix]) return withoutNumericSuffix;

    const alphaPrefix = raw.match(/^[a-z]+/)?.[0] || '';
    if (alphaPrefix && NODE_TYPES[alphaPrefix]) return alphaPrefix;

    return raw;
}

function isHistoryConversationMessage(message: ChatMessage): boolean {
    if (!message) return false;
    if (message.sender === 'console') return false;
    return message.role === 'user' || message.role === 'agent';
}

function clearRunActivity(runPatch: Partial<RunRecord> & Pick<RunRecord, 'id'>): Partial<RunRecord> & Pick<RunRecord, 'id'> {
    return {
        ...runPatch,
        current_node_execution_id: '',
        active_node_execution_ids: [],
    };
}

function connectorNodeStatusLabel(status: string, lang: 'en' | 'zh'): string {
    const normalized = String(status || '').trim().toLowerCase();
    const zhMap: Record<string, string> = {
        queued: '排队中',
        running: '执行中',
        passed: '已完成',
        failed: '失败',
        blocked: '阻塞',
        waiting_approval: '等待审批',
        skipped: '已跳过',
        cancelled: '已取消',
    };
    const enMap: Record<string, string> = {
        queued: 'Queued',
        running: 'Running',
        passed: 'Passed',
        failed: 'Failed',
        blocked: 'Blocked',
        waiting_approval: 'Awaiting approval',
        skipped: 'Skipped',
        cancelled: 'Cancelled',
    };
    return (lang === 'zh' ? zhMap : enMap)[normalized] || normalized || (lang === 'zh' ? '未知' : 'Unknown');
}

function connectorReviewLabel(decision: string, lang: 'en' | 'zh'): string {
    const normalized = String(decision || '').trim().toLowerCase();
    const zhMap: Record<string, string> = {
        approve: '通过',
        reject: '驳回',
        needs_fix: '需修复',
        blocked: '阻塞',
    };
    const enMap: Record<string, string> = {
        approve: 'Approved',
        reject: 'Rejected',
        needs_fix: 'Needs fix',
        blocked: 'Blocked',
    };
    return (lang === 'zh' ? zhMap : enMap)[normalized] || normalized || (lang === 'zh' ? '未知' : 'Unknown');
}

function connectorValidationLabel(summaryStatus: string, lang: 'en' | 'zh'): string {
    const normalized = String(summaryStatus || '').trim().toLowerCase();
    const zhMap: Record<string, string> = {
        passed: '通过',
        failed: '失败',
        blocked: '阻塞',
        skipped: '跳过',
    };
    const enMap: Record<string, string> = {
        passed: 'Passed',
        failed: 'Failed',
        blocked: 'Blocked',
        skipped: 'Skipped',
    };
    return (lang === 'zh' ? zhMap : enMap)[normalized] || normalized || (lang === 'zh' ? '未知' : 'Unknown');
}

// Preview validation
interface ClientPreviewValidation {
    ok: boolean; status: number; bytes: number; errors: string[]; warnings: string[];
}

interface WorkspaceUpdatedDetail {
    eventType: string;
    stage?: string;
    outputDir?: string;
    targetDir?: string;
    files?: string[];
    copiedFiles?: number;
    live?: boolean;
    final?: boolean;
    previewUrl?: string;
}

interface DesktopQaFrame {
    index?: number;
    label?: string;
    path?: string;
    stateHash?: string;
    state_hash?: string;
}

interface DesktopQaSessionResult {
    ok?: boolean;
    status?: string;
    sessionId?: string;
    session_id?: string;
    previewUrl?: string;
    preview_url?: string;
    scenario?: string;
    agent?: string;
    runId?: string;
    run_id?: string;
    nodeExecutionId?: string;
    node_execution_id?: string;
    startedAt?: string;
    endedAt?: string;
    ended_at?: string;
    frames?: DesktopQaFrame[];
    videoPath?: string;
    video_path?: string;
    timelapsePath?: string;
    timelapse_path?: string;
    rrwebRecordingPath?: string;
    rrweb_recording_path?: string;
    rrwebEventCount?: number;
    rrweb_event_count?: number;
    timelapseFrameCount?: number;
    timelapse_frame_count?: number;
    logPath?: string;
    log_path?: string;
    actions?: Array<Record<string, unknown>>;
    consoleErrors?: Array<Record<string, unknown>>;
    console_errors?: Array<Record<string, unknown>>;
    failedRequests?: Array<Record<string, unknown>>;
    failed_requests?: Array<Record<string, unknown>>;
    pageErrors?: Array<Record<string, unknown>>;
    page_errors?: Array<Record<string, unknown>>;
    summary?: string;
    error?: string;
}

async function runClientPreviewValidation(previewUrl: string): Promise<ClientPreviewValidation> {
    const result: ClientPreviewValidation = { ok: false, status: 0, bytes: 0, errors: [], warnings: [] };
    try {
        const resp = await fetch(previewUrl, { cache: 'no-store' });
        result.status = resp.status;
        const html = await resp.text();
        result.bytes = new TextEncoder().encode(html).length;
        const lower = html.toLowerCase();
        if (resp.status !== 200) result.errors.push(`Preview HTTP status is ${resp.status}, expected 200`);
        if (!lower.includes('<!doctype html>')) result.errors.push('Missing <!DOCTYPE html>');
        if (!lower.includes('<html')) result.errors.push('Missing <html> tag');
        if (!lower.includes('<head')) result.errors.push('Missing <head> tag');
        if (!lower.includes('<body')) result.errors.push('Missing <body> tag');
        if (!lower.includes('<style')) result.warnings.push('No inline <style> block found');
        if (!lower.includes('@media')) result.warnings.push('No @media responsive rules found');
        if (result.bytes < 1200) result.errors.push(`HTML too small (${result.bytes} bytes), maybe truncated`);
        result.ok = result.errors.length === 0;
    } catch (e) {
        result.errors.push(`Preview fetch failed: ${e}`);
    }
    return result;
}

function emitWorkspaceUpdated(detail: WorkspaceUpdatedDetail): void {
    if (typeof window === 'undefined') return;
    try {
        window.dispatchEvent(new CustomEvent<WorkspaceUpdatedDetail>('evermind:workspace-updated', { detail }));
    } catch {
        /* noop */
    }
}

// ── Hook Options ──
export interface UseRuntimeConnectionOptions {
    wsUrl: string;
    lang: 'en' | 'zh';
    difficulty: 'simple' | 'standard' | 'pro';
    goalRuntime?: 'local' | 'openclaw';
    sessionId?: string;
    messages: ChatMessage[];
    addMessage: (role: 'user' | 'system' | 'agent', content: string, sender?: string, icon?: string, borderColor?: string, completionData?: import('@/lib/types').ChatMessage['completionData'], attachments?: ChatAttachment[]) => void;
    addReport: (report: RunReportRecord) => void;
    buildPlanNodes: (
        subtasks: Array<{ id: string; agent: string; task: string; depends_on: string[] }>,
        lang: 'en' | 'zh',
    ) => Record<string, string>;
    updateNodeData: (nodeId: string, data: Record<string, unknown>) => void;
    nodes: Node[];
    edges: RFEdge[];
    setNodes: (nodes: Node[] | ((prev: Node[]) => Node[])) => void;

    // ── P0-1: Merge callbacks for canonical state ──
    /** Merge a partial task update into the canonical task list (e.g. review verdict, selfcheck items). */
    onMergeTask?: (task: Partial<TaskCard> & Pick<TaskCard, 'id'>) => void;
    /** Merge a run update into the canonical run list (e.g. status change, completion). */
    onMergeRun?: (run: Partial<RunRecord> & Pick<RunRecord, 'id'>) => void;
    /** Merge a node execution update into the canonical NE list. */
    onMergeNodeExecution?: (ne: Partial<NodeExecutionRecord> & Pick<NodeExecutionRecord, 'id' | 'run_id'>) => void;
    /** Active OpenClaw runs to re-check when the WS reconnects. */
    reconnectRunIds?: string[];
    /** Feed connector/session events to UI consumers such as OpenClawPanel. */
    onConnectorEvent?: (event: {
        type: string;
        label: string;
        detail?: string;
        timestamp: number;
    }) => void;
}

export interface UseRuntimeConnectionReturn {
    running: boolean;
    previewUrl: string | null;
    previewRunId: string | null;
    canvasView: 'editor' | 'preview' | 'files';
    setCanvasView: React.Dispatch<React.SetStateAction<'editor' | 'preview' | 'files'>>;
    connected: boolean;
    wsRef: React.RefObject<WebSocket | null>;
    handleSendGoal: (goal: string, attachments?: ChatAttachment[]) => void;
    handleRun: () => void;
    handleStop: () => void;
    setPreviewUrl: React.Dispatch<React.SetStateAction<string | null>>;
    /** OpenClaw V1: dispatch a node to OpenClaw via WS */
    dispatchNode: (payload: Record<string, unknown>) => void;
    /** OpenClaw V1: cancel a run */
    cancelRunWS: (runId: string) => void;
    /** OpenClaw V1: resume a run */
    resumeRunWS: (runId: string) => void;
    /** OpenClaw V1: rerun a node */
    rerunNodeWS: (neId: string) => void;
    /** P1-2C: Re-dispatch stale nodes on WS reconnect */
    recheckStaleNodes: (runId: string) => Promise<void>;
    /** Manual WS reconnect for connector UI. */
    reconnect: () => void;
    /** Backend runtime/session metadata shown in desktop connector UI. */
    connectorRuntimeId: string;
    connectorPid: string;
    connectorConnectedAt: number | null;
    connectorLastEventAt: number | null;
}

export function useRuntimeConnection({
    wsUrl, lang, difficulty, goalRuntime = 'local', sessionId = '', messages, addMessage, addReport,
    buildPlanNodes, updateNodeData, nodes, edges, setNodes,
    onMergeTask, onMergeRun, onMergeNodeExecution, reconnectRunIds = [], onConnectorEvent,
}: UseRuntimeConnectionOptions): UseRuntimeConnectionReturn {
    const [running, setRunning] = useState(false);
    const [previewUrl, setPreviewUrl] = useState<string | null>(null);
    const [previewRunId, setPreviewRunId] = useState<string | null>(null);
    const [canvasView, setCanvasView] = useState<'editor' | 'preview' | 'files'>('editor');
    const [connectorRuntimeId, setConnectorRuntimeId] = useState('');
    const [connectorPid, setConnectorPid] = useState('');
    const [connectorConnectedAt, setConnectorConnectedAt] = useState<number | null>(null);
    const [connectorLastEventAt, setConnectorLastEventAt] = useState<number | null>(null);

    // Run-scoped refs
    const subtaskNodeMap = useRef<Record<string, string>>({});
    const runStartedAtRef = useRef<number>(0);
    const previewReadyForRunRef = useRef<boolean>(false);
    const previewFallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const runPreviewUrlRef = useRef<string>('');
    const canonicalTaskIdRef = useRef<string>('');
    const canonicalRunIdRef = useRef<string>('');
    const runSubtasksRef = useRef<Record<string, {
        task?: string; agent?: string; agentType?: string; nodeKey?: string; nodeLabel?: string; output?: string; error?: string;
        status?: string; retries?: number; startedAt?: number; endedAt?: number;
        durationSeconds?: number; timelineEvents?: string[];
    }>>({});
    const browserModeNotifiedRef = useRef<Record<string, boolean>>({});
    const waitingAiLastNotifyRef = useRef<Record<string, number>>({});
    const waitingAiLastConsoleLogRef = useRef<Record<string, number>>({});
    const previousConnectedRef = useRef(false);
    const desktopQaSessionRef = useRef<Record<string, boolean>>({});

    const emitConnectorEvent = useCallback((type: string, label: string, detail?: string, timestamp = Date.now()) => {
        setConnectorLastEventAt(timestamp);
        onConnectorEvent?.({ type, label, detail, timestamp });
    }, [onConnectorEvent]);

    const appendSubtaskTimeline = useCallback((subtaskId: string, line: string) => {
        if (!subtaskId || !line.trim()) return;
        const prev = runSubtasksRef.current[subtaskId] || {};
        const events = [...(prev.timelineEvents || []), line.trim()].slice(-80);
        runSubtasksRef.current[subtaskId] = { ...prev, timelineEvents: events };
    }, []);

    const appendCanvasNodeLog = useCallback((canvasNodeId: string, entry: { ts?: number; msg: string; type?: string }) => {
        const message = String(entry.msg || '').trim();
        if (!canvasNodeId || !message) return;
        const ts = normalizeEpochMs(entry.ts, Date.now());
        const type = String(entry.type || 'info');
        setNodes((prev) => prev.map((node) => {
            if (node.id !== canvasNodeId) return node;
            const currentLog = Array.isArray(node.data?.log)
                ? (node.data.log as Array<{ ts?: number; msg?: string; type?: string }>)
                : [];
            const nextEntry = {
                ts,
                msg: message.slice(0, 520),
                type,
            };
            const last = currentLog[currentLog.length - 1];
            const duplicate = last
                && String(last.msg || '') === nextEntry.msg
                && String(last.type || 'info') === nextEntry.type
                && Math.abs(normalizeEpochMs(last.ts, 0) - nextEntry.ts) < 1500;
            const nextLog = duplicate ? currentLog : [...currentLog, nextEntry].slice(-80);
            return {
                ...node,
                data: {
                    ...node.data,
                    log: nextLog,
                },
            };
        }));
    }, [setNodes]);

    const mergeCanvasNodeLogs = useCallback((canvasNodeId: string, entries: Array<{ ts?: number; msg: string; type?: string }>) => {
        const sanitized = entries
            .map((entry) => ({
                ts: normalizeEpochMs(entry.ts, Date.now()),
                msg: String(entry.msg || '').trim().slice(0, 520),
                type: String(entry.type || 'info'),
            }))
            .filter((entry) => entry.msg);

        if (!canvasNodeId || sanitized.length === 0) return;

        setNodes((prev) => prev.map((node) => {
            if (node.id !== canvasNodeId) return node;
            const currentLog = Array.isArray(node.data?.log)
                ? (node.data.log as Array<{ ts?: number; msg?: string; type?: string }>)
                : [];
            const merged = [...currentLog];
            for (const nextEntry of sanitized) {
                const last = merged[merged.length - 1];
                const duplicate = last
                    && String(last.msg || '') === nextEntry.msg
                    && String(last.type || 'info') === nextEntry.type
                    && Math.abs(normalizeEpochMs(last.ts, 0) - nextEntry.ts) < 1500;
                if (!duplicate) {
                    merged.push(nextEntry);
                }
            }
            return {
                ...node,
                data: {
                    ...node.data,
                    log: merged.slice(-80),
                },
            };
        }));
    }, [setNodes]);

    const clearPreviewFallbackTimer = useCallback(() => {
        if (previewFallbackTimerRef.current) {
            clearTimeout(previewFallbackTimerRef.current);
            previewFallbackTimerRef.current = null;
        }
    }, []);

    const rememberPreviewRunId = useCallback((value: unknown) => {
        const nextRunId = String(value || '').trim();
        if (!nextRunId) return;
        setPreviewRunId((prev) => prev === nextRunId ? prev : nextRunId);
    }, []);

    const resolveCanvasNodeId = useCallback((payload: Record<string, unknown>) => {
        const nodeExecutionId = String(payload.nodeExecutionId || '').trim();
        const nodeKey = String(payload.nodeKey || '').trim();
        const normalizedNodeKey = normalizeCanvasNodeType(nodeKey);
        const subtaskId = String(payload.subtaskId || '').trim();
        const nodeLabel = String(payload.nodeLabel || '').trim();
        const rawNodeKeyLower = nodeKey.toLowerCase();
        const normalizedNodeKeyLower = normalizedNodeKey.toLowerCase();
        const hasSpecificNodeKey = Boolean(
            rawNodeKeyLower
            && normalizedNodeKeyLower
            && rawNodeKeyLower !== normalizedNodeKeyLower,
        );

        if (nodeExecutionId && subtaskNodeMap.current[nodeExecutionId]) {
            return subtaskNodeMap.current[nodeExecutionId];
        }
        if (subtaskId && subtaskNodeMap.current[subtaskId]) {
            return subtaskNodeMap.current[subtaskId];
        }

        const exactMatch = nodes.find((node) =>
            (nodeExecutionId && String(node.data?.nodeExecutionId || '').trim() === nodeExecutionId) ||
            (nodeKey && node.id === nodeKey) ||
            (nodeKey && String(node.data?.rawNodeKey || '').trim() === nodeKey) ||
            (subtaskId && String(node.data?.subtaskId || '').trim() === subtaskId),
        );
        if (exactMatch) return exactMatch.id;

        if (nodeKey) {
            const rawKeyMatches = nodes.filter((node) => {
                const nodeType = String(node.data?.nodeType || '').trim().toLowerCase();
                const rawNodeKey = String(node.data?.rawNodeKey || '').trim().toLowerCase();
                return nodeType === rawNodeKeyLower || rawNodeKey === rawNodeKeyLower;
            });
            if (rawKeyMatches.length === 1) return rawKeyMatches[0].id;

            // Never collapse builder1/builder2-style keys back onto a generic builder node.
            if (!hasSpecificNodeKey && normalizedNodeKeyLower) {
                const normalizedTypeMatches = nodes.filter((node) => {
                    const nodeType = String(node.data?.nodeType || '').trim().toLowerCase();
                    const rawNodeKey = String(node.data?.rawNodeKey || '').trim().toLowerCase();
                    return nodeType === normalizedNodeKeyLower || rawNodeKey === normalizedNodeKeyLower;
                });
                if (normalizedTypeMatches.length === 1) return normalizedTypeMatches[0].id;
            }
        }

        if (nodeLabel) {
            const labelMatches = nodes.filter((node) => String(node.data?.label || '').trim() === nodeLabel);
            if (labelMatches.length === 1) return labelMatches[0].id;
        }

        return null;
    }, [nodes]);

    useEffect(() => {
        return () => clearPreviewFallbackTimer();
    }, [clearPreviewFallbackTimer]);

    const resetRunState = useCallback(() => {
        runStartedAtRef.current = Date.now();
        previewReadyForRunRef.current = false;
        runPreviewUrlRef.current = '';
        canonicalTaskIdRef.current = '';
        canonicalRunIdRef.current = '';
        setPreviewRunId(null);
        runSubtasksRef.current = {};
        browserModeNotifiedRef.current = {};
        waitingAiLastNotifyRef.current = {};
        waitingAiLastConsoleLogRef.current = {};
        desktopQaSessionRef.current = {};
        clearPreviewFallbackTimer();
    }, [clearPreviewFallbackTimer]);

    // ── WS Message Handler ──
    const onWSMessage = useCallback((msg: Record<string, unknown>) => {
        const t = msg.type as string;
        const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);
        const addConsoleLog = (line: string) => {
            addMessage('system', line, 'console', '🪵', 'var(--text3)');
        };

        if (t === 'connected') {
            const version = String(msg.version || 'unknown');
            const runtimeId = String(msg.runtime_id || 'n/a');
            const pid = String(msg.pid || 'n/a');
            setConnectorRuntimeId(runtimeId === 'n/a' ? '' : runtimeId);
            setConnectorPid(pid === 'n/a' ? '' : pid);
            setConnectorConnectedAt(Date.now());
            addMessage('system',
                tr(`后端已连接：v${version} · runtime ${runtimeId} · pid ${pid}`, `Backend connected: v${version} · runtime ${runtimeId} · pid ${pid}`),
                'System', '🟢', 'var(--green)');
            addConsoleLog(`[connected] version=${version} runtime=${runtimeId} pid=${pid}`);
            emitConnectorEvent(
                'bridge_connected',
                tr('桌面桥接已连接', 'Desktop bridge connected'),
                `${runtimeId} · pid ${pid}`,
            );

        } else if (t === 'orchestrator_start') {
            setRunning(true);
            resetRunState();
            setPreviewUrl(null);
            setCanvasView('editor');
            addMessage('system', tr('已接收目标，正在规划执行...', 'Goal received. Planning execution...'), 'Orchestrator', '🧠');
            addConsoleLog(`[orchestrator_start] difficulty=${String(msg.difficulty || 'standard')}`);

        } else if (t === 'run_goal_ack') {
            // P0-1: Backend created canonical task/run/NEs — merge into frontend state
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const taskId = String(payload.taskId || '');
            const runId = String(payload.runId || '');
            const taskData = payload.task as Record<string, unknown> | undefined;
            const runData = payload.run as Record<string, unknown> | undefined;
            const nodeExecutions = (payload.nodeExecutions || []) as Array<Record<string, unknown>>;
            if (taskId) canonicalTaskIdRef.current = taskId;
            if (runId) canonicalRunIdRef.current = runId;
            addConsoleLog(`[run_goal_ack] taskId=${taskId} runId=${runId} NEs=${nodeExecutions.length}`);
            emitConnectorEvent(
                'task_created',
                tr('已创建规范任务', 'Canonical task created'),
                taskId && runId ? `${taskId} · ${runId}` : undefined,
                Date.now(),
            );

            // Merge task into board
            if (taskId && taskData && onMergeTask) {
                onMergeTask({ id: taskId, ...taskData } as Partial<TaskCard> & Pick<TaskCard, 'id'>);
            }
            // Merge run
            if (runId && runData && onMergeRun) {
                onMergeRun({ id: runId, ...runData } as Partial<RunRecord> & Pick<RunRecord, 'id'>);
            }
            // Merge NEs
            if (onMergeNodeExecution) {
                for (const ne of nodeExecutions) {
                    if (ne.id && ne.run_id) {
                        onMergeNodeExecution({ id: String(ne.id), run_id: String(ne.run_id), ...ne } as Partial<NodeExecutionRecord> & Pick<NodeExecutionRecord, 'id' | 'run_id'>);
                    }
                }
            }

            // §P0-1: Build canvas nodes for ALL custom plans (local AND openclaw).
            // Previously gated by `effectiveRuntime === 'openclaw'`, which caused
            // local custom runs (dispatched by OpenClaw) to show an empty canvas.
            const effectiveRuntime = String(payload.effectiveRuntime || payload.requestedRuntime || '');
            if (nodeExecutions.length > 0) {
                // V4.3.1 FIX: Check if plan_created already created canvas nodes.
                // If so, map NE IDs to existing nodes instead of rebuilding the
                // entire canvas — rebuilding would reset "running" nodes back to
                // "queued", causing the 排队中 display bug.
                const existingSubtaskNodes = Object.keys(subtaskNodeMap.current).length > 0;
                const hasExistingCanvasNodes = nodes.some((node) =>
                    String(node.data?.subtaskId || '').trim()
                    || String(node.data?.nodeExecutionId || '').trim()
                    || String(node.data?.rawNodeKey || '').trim(),
                );

                if (existingSubtaskNodes || hasExistingCanvasNodes) {
                    // Canvas nodes already exist from plan_created — just map NE IDs
                    // to existing canvas nodes and add NE metadata without rebuilding.
                    for (let i = 0; i < nodeExecutions.length; i++) {
                        const neId = String(nodeExecutions[i].id || '').trim();
                        const subtaskId = String(i + 1);
                        const canvasNodeId = subtaskNodeMap.current[subtaskId]
                            || subtaskNodeMap.current[neId];
                        if (canvasNodeId && neId) {
                            subtaskNodeMap.current[neId] = canvasNodeId;
                            subtaskNodeMap.current[subtaskId] = canvasNodeId;
                        }
                    }
                } else {
                    const subtaskFormat = nodeExecutions.map(ne => ({
                        id: String(ne.id || ''),
                        agent: normalizeCanvasNodeType(ne.node_key || 'builder'),
                        task: String(ne.inputSummary || ne.input_summary || ne.node_label || ne.node_key || ''),
                        depends_on: Array.isArray(ne.depends_on_keys) ? ne.depends_on_keys.map(String) : [],
                    }));
                    // Map NE depends_on_keys (node_keys) to NE ids for edge building
                    const keyToId: Record<string, string> = {};
                    for (const ne of nodeExecutions) {
                        if (ne.node_key && ne.id) keyToId[String(ne.node_key)] = String(ne.id);
                    }
                    const subtasksWithIdDeps = subtaskFormat.map(st => ({
                        ...st,
                        depends_on: st.depends_on.map(key => keyToId[key] || key),
                    }));
                    subtaskNodeMap.current = buildPlanNodes(subtasksWithIdDeps, lang);

                    // §P0-3: Also build a subtask-index → NE-id mapping so that local
                    // orchestrator events (using subtask IDs "1","2","3"...) can find
                    // the correct canvas node via NE id.
                    for (let i = 0; i < nodeExecutions.length; i++) {
                        const neId = String(nodeExecutions[i].id || '').trim();
                        const subtaskId = String(i + 1);  // orchestrator subtask IDs are 1-indexed
                        if (neId && subtaskNodeMap.current[neId]) {
                            // Map subtask ID → same canvas node as the NE ID
                            subtaskNodeMap.current[subtaskId] = subtaskNodeMap.current[neId];
                        }
                    }
                }

                // Update NE metadata on canvas nodes. Use status from NE only
                // if it is more advanced than the current canvas status — never
                // downgrade a "running" node back to "queued".
                const STATUS_RANK: Record<string, number> = {
                    idle: 0, queued: 1, running: 2, waiting_approval: 3,
                    passed: 4, failed: 4, blocked: 4, cancelled: 4, skipped: 4,
                };
                for (const ne of nodeExecutions) {
                    const neId = String(ne.id || '').trim();
                    const canvasNodeId = neId ? subtaskNodeMap.current[neId] : '';
                    if (!canvasNodeId) continue;
                    const startedAt = normalizeEpochMs(ne.startedAt ?? ne.started_at, 0);
                    const endedAt = normalizeEpochMs(ne.endedAt ?? ne.ended_at, 0);
                    const durationSeconds = Number.isFinite(Number(ne.durationSeconds ?? ne.duration_seconds))
                        ? Math.max(0, Number(ne.durationSeconds ?? ne.duration_seconds))
                        : deriveDurationSeconds(startedAt, endedAt);
                    const progress = Number(ne.progress);
                    const tokensUsed = Number(ne.tokensUsed ?? ne.tokens_used);
                    const cost = Number(ne.cost ?? 0);
                    const neStatus = String(ne.status || 'queued').trim() || 'queued';
                    const existingNode = nodes.find((node) => node.id === canvasNodeId);
                    const existingStatus = String(existingNode?.data?.status || 'idle');
                    // Only update status if NE status is more advanced (never downgrade)
                    const shouldUpdateStatus = (STATUS_RANK[neStatus] ?? 0) >= (STATUS_RANK[existingStatus] ?? 0);
                    updateNodeData(canvasNodeId, {
                        nodeExecutionId: neId,
                        rawNodeKey: String(ne.node_key || '').trim(),
                        nodeType: normalizeCanvasNodeType(ne.node_key || 'builder'),
                        label: String(ne.node_label || ne.node_key || '').trim(),
                        ...(shouldUpdateStatus ? { status: neStatus } : {}),
                        runtime: effectiveRuntime === 'openclaw' ? 'openclaw' : 'local',
                        ...(Number.isFinite(progress) ? { progress } : {}),
                        ...(String(ne.assignedModel || ne.assigned_model || '').trim()
                            ? { assignedModel: String(ne.assignedModel || ne.assigned_model || '').trim() }
                            : {}),
                        ...(String(ne.inputSummary || ne.input_summary || '').trim()
                            ? { taskDescription: String(ne.inputSummary || ne.input_summary || '').trim() }
                            : {}),
                        ...(String(ne.outputSummary || ne.output_summary || '').trim()
                            ? {
                                outputSummary: String(ne.outputSummary || ne.output_summary || '').trim(),
                                lastOutput: String(ne.outputSummary || ne.output_summary || '').trim(),
                            }
                            : {}),
                        ...(Number.isFinite(tokensUsed) ? { tokensUsed } : {}),
                        ...(Number.isFinite(cost) ? { cost } : {}),
                        ...(startedAt > 0 ? { startedAt } : {}),
                        ...(endedAt > 0 ? { endedAt } : {}),
                        ...(durationSeconds !== undefined ? { durationSeconds } : {}),
                    });
                }
            }

            // Add visible chat message for task creation
            const taskTitle = String(taskData?.title || taskData?.id || taskId || '').slice(0, 80);
            addMessage('system',
                effectiveRuntime === 'openclaw'
                    ? tr(`☁ OpenClaw 已创建任务: ${taskTitle} (${nodeExecutions.length} 节点)`,
                         `☁ OpenClaw created task: ${taskTitle} (${nodeExecutions.length} nodes)`)
                    : tr(`✅ 任务已创建: ${taskTitle}`, `✅ Task created: ${taskTitle}`),
                effectiveRuntime === 'openclaw' ? 'OpenClaw' : 'System',
                effectiveRuntime === 'openclaw' ? 'OC' : '✅',
                effectiveRuntime === 'openclaw' ? '#a855f7' : '#22c55e');

        } else if (t === 'task_created') {
            // P0-1: Another client created a task — merge into board
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const taskData = payload.task as Record<string, unknown> | undefined;
            if (taskData && taskData.id && onMergeTask) {
                onMergeTask({ id: String(taskData.id), ...taskData } as Partial<TaskCard> & Pick<TaskCard, 'id'>);
                addConsoleLog(`[task_created] taskId=${String(taskData.id)}`);
                emitConnectorEvent(
                    'task_created',
                    tr(`任务已创建: ${String(taskData.title || taskData.id).slice(0, 60)}`, `Task created: ${String(taskData.title || taskData.id).slice(0, 60)}`),
                    String(taskData.id),
                );
            }

        } else if (t === 'plan_created') {
            const subtasks = (msg.subtasks as Array<{ id: string; agent: string; task: string; depends_on: string[] }>) || [];
            addMessage('system',
                tr(`计划已创建：${msg.total} 个步骤，开始自动执行`, `Plan ready: ${msg.total} steps. Starting automatic execution...`),
                'Plan', '📋');
            try { console.info('[Evermind][Plan]', subtasks); } catch { /* noop */ }
            addConsoleLog(`[plan_created] total=${String(msg.total || subtasks.length)}`);
            subtasks.forEach(st => {
                addConsoleLog(`[plan] #${st.id} ${st.agent} deps=[${(st.depends_on || []).join(',')}] task=${(st.task || '').slice(0, 120)}`);
            });
            const hasCanonicalCanvasNodes = nodes.some((node) =>
                String(node.data?.nodeExecutionId || '').trim()
                || String(node.data?.rawNodeKey || '').trim(),
            );
            if (hasCanonicalCanvasNodes) {
                addConsoleLog('[plan_created] preserving canonical canvas nodes');
            } else {
                subtaskNodeMap.current = buildPlanNodes(subtasks, lang);
            }

        } else if (t === 'subtask_start') {
            const subtaskId = msg.subtask_id as string;
            const agentName = String(msg.agent || 'agent');
            const nodeKey = String(msg.node_key || msg.nodeKey || '').trim();
            const nodeLabel = String(msg.node_label || msg.nodeLabel || '').trim();
            const displayName = nodeLabel || nodeKey || agentName;
            const startedAt = Date.now();
            runSubtasksRef.current[subtaskId] = {
                ...(runSubtasksRef.current[subtaskId] || {}),
                agent: displayName, agentType: agentName, nodeKey, nodeLabel, task: String(msg.task || ''),
                status: 'running', startedAt, endedAt: undefined, durationSeconds: undefined,
            };
            appendSubtaskTimeline(subtaskId, `开始执行：${displayName} 接收任务并进入处理流程。`);
            if (String(msg.task || '').trim()) {
                appendSubtaskTimeline(subtaskId, `任务说明：${String(msg.task || '').trim().slice(0, 240)}`);
            }
            // P1-3: Humanized agent names for feed
            const agentLabel = displayName.replace(/([a-z])([A-Z])/g, '$1 $2');
            const humanLabel = {
                builder: tr('代码生成', 'Code generation'),
                merger: tr('合并整合', 'Merge integration'),
                reviewer: tr('代码审核', 'Code review'),
                tester: tr('自动测试', 'Automated testing'),
                deployer: tr('自动部署', 'Auto-deployment'),
                validator: tr('质量验证', 'Quality validation'),
                planner: tr('任务规划', 'Task planning'),
            }[(nodeKey || agentName).toLowerCase()] || `${agentLabel} step`;
            addMessage('system', tr(`${humanLabel}开始`, `${humanLabel} started`), `${displayName} #${subtaskId}`, '⚙️', 'var(--blue)');
            addConsoleLog(`[subtask_start] #${subtaskId} agent=${agentName} node=${nodeKey || '-'} task=${String(msg.task || '').slice(0, 180)}`);
            const canvasNodeId = subtaskNodeMap.current[subtaskId];
            if (canvasNodeId) updateNodeData(canvasNodeId, {
                status: 'running', progress: 5, startedAt,
                outputSummary: tr(`${displayName} 正在接收任务并开始处理...`, `${displayName} received task, starting work...`),
                taskDescription: String(msg.task || ''),
            });

        } else if (t === 'subtask_complete') {
            const subtaskId = msg.subtask_id as string;
            const success = msg.success as boolean;
            const retryPending = Boolean(msg.retry_pending || msg.retryPending);
            const agentName = (msg.agent as string) || 'Agent';
            const nodeKey = String(msg.node_key || msg.nodeKey || '').trim();
            const nodeLabel = String(msg.node_label || msg.nodeLabel || '').trim();
            const displayName = nodeLabel || nodeKey || agentName;
            const fullOutput = ((msg.full_output || msg.output_preview || '') as string);
            const err = String(msg.error || '');
            const blocked = !success && !retryPending && /Blocked by failed dependencies/i.test(err);
            const prevState = runSubtasksRef.current[subtaskId] || {};
            const endedAt = Date.now();
            const startedAt = prevState.startedAt || endedAt;
            const durationSeconds = Math.max(0, Math.round((endedAt - startedAt) / 1000));
            runSubtasksRef.current[subtaskId] = {
                ...prevState, agent: displayName, agentType: agentName, nodeKey, nodeLabel,
                status: success ? 'completed' : (retryPending ? 'running' : (blocked ? 'blocked' : 'failed')),
                output: fullOutput, error: err, endedAt, durationSeconds,
            };
            appendSubtaskTimeline(subtaskId, success
                ? `执行完成：${displayName} 已完成，耗时约 ${durationSeconds} 秒。`
                : (retryPending
                    ? `本轮失败：${displayName} 在 ${durationSeconds} 秒后失败，系统将自动重试。`
                : (blocked
                    ? `执行阻断：${displayName} 未执行，因上游依赖失败而被阻断，耗时约 ${durationSeconds} 秒。`
                    : `执行失败：${displayName} 结束于失败，耗时约 ${durationSeconds} 秒。`)));
            if (err) appendSubtaskTimeline(subtaskId, `失败原因：${err.slice(0, 160)}`);
            // P1-3: Humanized completion messages
            const completeLabel = {
                builder: tr('代码生成', 'Code generation'),
                merger: tr('合并整合', 'Merge integration'),
                reviewer: tr('代码审核', 'Code review'),
                tester: tr('自动测试', 'Automated testing'),
                deployer: tr('自动部署', 'Auto-deployment'),
                validator: tr('质量验证', 'Quality validation'),
                planner: tr('任务规划', 'Task planning'),
            }[(nodeKey || agentName).toLowerCase()] || displayName;
            addMessage('system',
                success
                    ? tr(`${completeLabel}完成（${durationSeconds}s）`, `${completeLabel} completed (${durationSeconds}s)`)
                    : (retryPending
                        ? tr(`${completeLabel}本轮失败，准备重试`, `${completeLabel} attempt failed, retrying`)
                    : (blocked
                        ? tr(`${completeLabel}被上游失败阻断`, `${completeLabel} was blocked by an upstream failure`)
                        : tr(`${completeLabel}遇到问题`, `${completeLabel} encountered an issue`))),
                `${displayName} #${subtaskId}`,
                success ? '✅' : (retryPending ? '🔁' : (blocked ? '⛔' : '❌')),
                success ? 'var(--green)' : (retryPending ? 'var(--orange)' : (blocked ? 'var(--orange)' : 'var(--red)')));
            try { console.info(`[Evermind][Subtask ${subtaskId}]`, { agent: agentName, nodeKey, nodeLabel, success, output_len: fullOutput.length, output_preview: fullOutput.slice(0, 1200) }); } catch { /* noop */ }
            addConsoleLog(`[subtask_complete] #${subtaskId} agent=${agentName} node=${nodeKey || '-'} success=${String(success)} retry_pending=${String(retryPending)} output_len=${fullOutput.length}${err ? ` error=${err.slice(0, 160)}` : ''}`);
            let canvasNodeId = subtaskNodeMap.current[subtaskId];
            // Fallback: if the map lookup fails (e.g. plan_created was missed or IDs drifted),
            // try to find the canvas node by matching subtaskId stored in node data.
            if (!canvasNodeId) {
                const fallbackNode = nodes.find((n) => {
                    const d = n.data as Record<string, unknown>;
                    return String(d.subtaskId || '').trim() === subtaskId
                        || String(d.nodeExecutionId || '').trim() === subtaskId;
                });
                if (fallbackNode) {
                    canvasNodeId = fallbackNode.id;
                    subtaskNodeMap.current[subtaskId] = canvasNodeId;
                }
            }
            if (canvasNodeId) {
                // Build human-readable completion summary
                const completeSummary = success
                    ? tr(
                        `${completeLabel}已完成，耗时 ${durationSeconds} 秒。${fullOutput.length > 0 ? '已产出工作成果。' : ''}`,
                        `${completeLabel} completed in ${durationSeconds}s.${fullOutput.length > 0 ? ' Output generated.' : ''}`
                    )
                    : (retryPending
                        ? tr(
                            `${completeLabel}本轮执行失败，系统正在自动重试。${err ? `原因：${err.slice(0, 100)}` : ''}`,
                            `${completeLabel} attempt failed and is retrying automatically.${err ? ` Reason: ${err.slice(0, 100)}` : ''}`
                        )
                    : (blocked
                        ? tr(
                            `${completeLabel}未实际执行，因为上游节点已失败。${err ? `原因：${err.slice(0, 100)}` : ''}`,
                            `${completeLabel} did not execute because an upstream node failed.${err ? ` Reason: ${err.slice(0, 100)}` : ''}`
                        )
                        : tr(
                            `${completeLabel}执行失败。${err ? `原因：${err.slice(0, 100)}` : ''}`,
                            `${completeLabel} failed.${err ? ` Reason: ${err.slice(0, 100)}` : ''}`
                        )));
                // Collect timeline log
                const timelineEvents = (runSubtasksRef.current[subtaskId]?.timelineEvents || [])
                    .map((msg: string) => ({ ts: Date.now(), msg, type: success ? 'ok' : ((blocked || retryPending) ? 'warn' : 'error') }));
                mergeCanvasNodeLogs(canvasNodeId, timelineEvents);
                // CC-1: Accumulate tokens across retries instead of overwriting
                const existingNode = nodes.find((node) => node.id === canvasNodeId);
                const existingData = (existingNode?.data || {}) as Record<string, unknown>;
                const isRetried = (prevState.retries || 0) > 0;
                const prevTokens = isRetried ? Number(existingData.tokensUsed || 0) : 0;
                const prevPrompt = isRetried ? Number(existingData.promptTokens || 0) : 0;
                const prevCompletion = isRetried ? Number(existingData.completionTokens || 0) : 0;
                const prevCost = isRetried ? Number(existingData.cost || 0) : 0;
                updateNodeData(canvasNodeId, {
                    status: success ? 'passed' : (retryPending ? 'running' : (blocked ? 'blocked' : 'failed')),
                    progress: success ? 100 : (retryPending ? 45 : 100),
                    _terminalStatus: !retryPending,
                    lastOutput: fullOutput.substring(0, 4000),
                    outputSummary: completeSummary,
                    endedAt,
                    startedAt: prevState.startedAt || endedAt,
                    durationSeconds,
                    tokensUsed: prevTokens + Number(msg.tokens_used || 0),
                    promptTokens: prevPrompt + Number(msg.prompt_tokens || 0),
                    completionTokens: prevCompletion + Number(msg.completion_tokens || 0),
                    cost: prevCost + Number(msg.cost || 0),
                });
            }

        } else if (t === 'files_created') {
            const files = (msg.files as string[]) || [];
            const outputDir = (msg.output_dir as string) || '/tmp/evermind_output';
            const sid = String(msg.subtask_id || '');
            const artifactSync = (msg.artifact_sync as Record<string, unknown> | undefined) || undefined;
            const codeLines = Number(msg.code_lines || 0);
            const totalLines = Number(msg.total_lines || 0);
            const codeKb = Number(msg.code_kb || 0);
            const languages = Array.isArray(msg.languages) ? (msg.languages as string[]) : [];
            if (files.length > 0) {
                if (sid) {
                    const compactFiles = files.slice(0, 6).map((file) => file.split('/').pop()).join(', ');
                    appendSubtaskTimeline(sid, tr(`生成文件：${compactFiles}${files.length > 6 ? ' 等' : ''}。输出目录：${outputDir}`, `Generated files: ${compactFiles}${files.length > 6 ? ' and more' : ''}. Output dir: ${outputDir}`));
                    const canvasNodeId = subtaskNodeMap.current[sid];
                    if (canvasNodeId) {
                        appendCanvasNodeLog(canvasNodeId, {
                            ts: Date.now(),
                            msg: tr(`已写出 ${files.length} 个文件到 ${outputDir}`, `Wrote ${files.length} file(s) to ${outputDir}`),
                            type: 'ok',
                        });
                        if (codeLines > 0) {
                            updateNodeData(canvasNodeId, {
                                codeLines,
                                totalLines,
                                codeKb,
                                codeLanguages: languages,
                            });
                        }
                    }
                }
                addMessage('system',
                    tr(`产物已更新：${files.length} 个文件（目录：<code>${outputDir}</code>）`, `Artifacts updated: ${files.length} files (dir: <code>${outputDir}</code>)`),
                    'File Output', '📁', 'var(--green)');
                addConsoleLog(`[files_created] count=${files.length} dir=${outputDir}${codeLines > 0 ? ` code_lines=${codeLines} code_kb=${codeKb}` : ''}`);
            }
            emitWorkspaceUpdated({
                eventType: 'files_created',
                outputDir,
                files,
                targetDir: String(artifactSync?.target_dir || ''),
                copiedFiles: Number(artifactSync?.copied_files || 0),
                live: Boolean(artifactSync?.live),
            });

        } else if (t === 'preview_ready') {
            const rawPreviewUrl = msg.preview_url as string;
            const files = (msg.files as string[]) || [];
            const sid = String(msg.subtask_id || '');
            const artifactSync = (msg.artifact_sync as Record<string, unknown> | undefined) || undefined;
            if (rawPreviewUrl) {
                clearPreviewFallbackTimer();
                let resolvedUrl = rawPreviewUrl.trim();
                try { resolvedUrl = new URL(resolvedUrl, 'http://127.0.0.1:8765').toString(); } catch { /* Keep original */ }
                const isFinalPreview = Boolean(msg.final);
                const hadPreviewAlready = previewReadyForRunRef.current;
                const shouldActivatePreview = isFinalPreview || !hadPreviewAlready;
                runPreviewUrlRef.current = resolvedUrl;
                if (sid) {
                    appendSubtaskTimeline(
                        sid,
                        shouldActivatePreview
                            ? tr(`预览已就绪：${resolvedUrl}`, `Preview ready: ${resolvedUrl}`)
                            : tr(`新的预览版本已写出，但为避免运行中反复刷新，当前预览视图保持不自动切换。`, `A newer preview artifact was generated, but the current preview was kept stable to avoid repeated refresh during the run.`),
                    );
                    const canvasNodeId = subtaskNodeMap.current[sid];
                    if (canvasNodeId) {
                        appendCanvasNodeLog(canvasNodeId, {
                            ts: Date.now(),
                            msg: shouldActivatePreview
                                ? tr(`预览地址已生成：${resolvedUrl}`, `Preview URL ready: ${resolvedUrl}`)
                                : tr(`新的预览版本已生成，已延后自动刷新。`, `A newer preview artifact was generated; auto-refresh was deferred.`),
                            type: 'ok',
                        });
                    }
                }
                if (shouldActivatePreview) {
                    previewReadyForRunRef.current = true;
                    const safePreviewUrl = escapeHtml(resolvedUrl);
                    const previewWithBust = withCacheBust(resolvedUrl);
                    setPreviewUrl(previewWithBust);
                    setCanvasView('preview');
                    const shortFiles = files.slice(0, 3).map((f: string) => f.split('/').pop()).join(', ');
                    addMessage('system',
                        tr(
                            `<b>预览已就绪</b>，已自动切换到预览视图。<br/><a href="${safePreviewUrl}" target="_blank" rel="noopener noreferrer">新窗口打开</a>${shortFiles ? `<br/>文件: ${shortFiles}` : ''}`,
                            `<b>Preview ready</b>, switched to preview view.<br/><a href="${safePreviewUrl}" target="_blank" rel="noopener noreferrer">Open in new window</a>${shortFiles ? `<br/>Files: ${shortFiles}` : ''}`),
                        'Preview', '🔗', 'var(--green)');
                    void (async () => {
                        const check = await runClientPreviewValidation(resolvedUrl);
                        if (check.ok) {
                            addMessage('system', tr(`预览验收通过（HTTP ${check.status}，${check.bytes} bytes）`, `Preview validation passed (HTTP ${check.status}, ${check.bytes} bytes)`), 'Validator', '✅', 'var(--green)');
                        } else {
                            addMessage('system', tr(`预览验收失败：${check.errors.slice(0, 3).join('；')}`, `Preview validation failed: ${check.errors.slice(0, 3).join('; ')}`), 'Validator', '❌', 'var(--red)');
                        }
                    })();
                }
                try { console.info('[Evermind][PreviewReady]', { preview_url: resolvedUrl, files, final: isFinalPreview, activated: shouldActivatePreview }); } catch { /* noop */ }
                addConsoleLog(`[preview_ready] url=${resolvedUrl} files=${files.length} final=${String(isFinalPreview)} activated=${String(shouldActivatePreview)}`);
                emitWorkspaceUpdated({
                    eventType: 'preview_ready',
                    outputDir: String(msg.output_dir || ''),
                    files,
                    targetDir: String(artifactSync?.target_dir || ''),
                    copiedFiles: Number(artifactSync?.copied_files || 0),
                    live: Boolean(artifactSync?.live),
                    final: isFinalPreview,
                    previewUrl: resolvedUrl,
                });
            }

        } else if (t === 'subtask_retry') {
            const retryCount = Number(msg.retry || 0);
            const sid = String(msg.subtask_id || '');
            if (sid) {
                runSubtasksRef.current[sid] = { ...(runSubtasksRef.current[sid] || {}), retries: retryCount, status: 'retrying' };
                const retryReason = String(msg.error || '').slice(0, 140);
                appendSubtaskTimeline(sid, retryReason
                    ? `触发重试：第 ${retryCount}/${String(msg.max_retries || '?')} 次，原因：${retryReason}`
                    : `触发重试：第 ${retryCount}/${String(msg.max_retries || '?')} 次。`);
            }
            addMessage('system', tr(`触发重试（第 ${msg.retry}/${msg.max_retries} 次）`, `Retry triggered (attempt ${msg.retry}/${msg.max_retries})`), `Retry #${msg.subtask_id}`, '🔄', 'var(--yellow)');
            addConsoleLog(`[subtask_retry] #${String(msg.subtask_id)} retry=${String(msg.retry)}/${String(msg.max_retries)} error=${String(msg.error || '').slice(0, 180)}`);
            const canvasNodeId = subtaskNodeMap.current[msg.subtask_id as string];
            if (canvasNodeId) {
                const retryStartedAt = Date.now();
                // Reset startedAt and timer on retry so the clock restarts
                if (sid) {
                    runSubtasksRef.current[sid] = {
                        ...(runSubtasksRef.current[sid] || {}),
                        startedAt: retryStartedAt,
                        endedAt: undefined,
                        durationSeconds: undefined,
                    };
                }
                updateNodeData(canvasNodeId, {
                    status: 'running',
                    progress: 5,
                    startedAt: retryStartedAt,
                    durationSeconds: 0,
                    _terminalStatus: false,
                });
            }

        } else if (t === 'test_failed_retrying') {
            addMessage('system', tr('测试未通过，正在回滚并重试修复', 'Tests failed, rerunning with repair instructions'), 'Tester', '🧪', 'var(--red)');

        } else if (t === 'orchestrator_complete') {
            setRunning(false);
            const success = msg.success as boolean;
            const subtasks = (msg.subtasks as Array<{
                id: string; agent: string; status: string; retries: number;
                task?: string; output_preview?: string; error?: string;
                work_summary?: string[]; files_created?: string[];
            }>) || [];
            const diffRaw = String(msg.difficulty || difficulty).toLowerCase();
            const runDifficulty: 'simple' | 'standard' | 'pro' = (
                diffRaw === 'simple' || diffRaw === 'pro' || diffRaw === 'standard'
            ) ? diffRaw : 'standard';

            const reportRecord: RunReportRecord = {
                id: `run_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
                createdAt: Date.now(),
                goal: String(msg.goal || messages.slice().reverse().find((m) => m.role === 'user')?.content || ''),
                difficulty: runDifficulty, success,
                totalSubtasks: Number(msg.total_subtasks || 0),
                completed: Number(msg.completed || 0),
                failed: Number(msg.failed || 0),
                totalRetries: Number(msg.total_retries || 0),
                durationSeconds: Number(msg.duration_seconds || 0),
                previewUrl: runPreviewUrlRef.current || undefined,
                taskId: canonicalTaskIdRef.current || undefined,
                runId: canonicalRunIdRef.current || undefined,
                subtasks: subtasks.map((st) => {
                    const runtime = runSubtasksRef.current[String(st.id)] || {};
                    return {
                        id: String(st.id), agent: String(st.agent || runtime.agent || 'agent'),
                        status: String(runtime.status || st.status || 'unknown'),
                        retries: Number(st.retries || runtime.retries || 0),
                        task: String(st.task || runtime.task || ''),
                        outputPreview: String(runtime.output || st.output_preview || '').slice(0, 2200),
                        error: String(runtime.error || st.error || '').slice(0, 900),
                        durationSeconds: Number.isFinite(Number(runtime.durationSeconds)) ? Number(runtime.durationSeconds) : undefined,
                        startedAt: Number.isFinite(Number(runtime.startedAt)) ? Number(runtime.startedAt) : undefined,
                        endedAt: Number.isFinite(Number(runtime.endedAt)) ? Number(runtime.endedAt) : undefined,
                        timelineEvents: Array.isArray(runtime.timelineEvents) ? runtime.timelineEvents.slice(-30) : undefined,
                        workSummary: Array.isArray(st.work_summary) ? st.work_summary : undefined,
                        filesCreated: Array.isArray(st.files_created) ? st.files_created : undefined,
                    };
                }),
            };
            addReport(reportRecord);

            // Persist report + auto-create task
            void (async () => {
                try {
                    const { saveReportApi } = await import('@/lib/api');
                    const reportPayload: Record<string, unknown> = {
                        ...reportRecord,
                        task_id: canonicalTaskIdRef.current || reportRecord.taskId || '',
                        run_id: canonicalRunIdRef.current || reportRecord.runId || '',
                        subtasks: reportRecord.subtasks.map((st) => ({ ...st, files_created: st.filesCreated || [] })),
                    };
                    await saveReportApi(reportPayload).catch(() => {});
                } catch { /* backend persistence failed */ }
            })();

            const completionSummary = String(msg.summary || '').trim();
            const completionData: import('@/lib/types').ChatMessage['completionData'] = {
                success,
                completed: Number(msg.completed || 0),
                total: Number(msg.total_subtasks || 0),
                retries: Number(msg.total_retries || 0),
                durationSeconds: Number(msg.duration_seconds || 0),
                difficulty: runDifficulty,
                subtasks: subtasks.map(st => ({
                    id: String(st.id),
                    agent: String(st.agent || 'agent'),
                    status: String(st.status || 'unknown'),
                    retries: Number(st.retries || 0),
                    filesCreated: Array.isArray(st.files_created) ? st.files_created : undefined,
                    workSummary: Array.isArray(st.work_summary) ? st.work_summary : undefined,
                    codeLines: Number((st as any).code_lines || 0) || undefined,
                    codeKb: Number((st as any).code_kb || 0) || undefined,
                    codeLanguages: Array.isArray((st as any).code_languages) ? (st as any).code_languages : undefined,
                })),
                previewUrl: runPreviewUrlRef.current || undefined,
            };

            addMessage('system',
                tr(
                    `<b>执行完成</b>：${msg.completed}/${msg.total_subtasks} 节点，耗时 ${msg.duration_seconds}s${completionSummary ? `<br/>${escapeHtml(completionSummary)}` : ''}`,
                    `<b>Run completed</b>: ${msg.completed}/${msg.total_subtasks} nodes, ${msg.duration_seconds}s${completionSummary ? `<br/>${escapeHtml(completionSummary)}` : ''}`),
                'Report', '🏁', success ? 'var(--green)' : 'var(--orange)', completionData);
            addConsoleLog(`[orchestrator_complete] success=${String(success)} completed=${String(msg.completed)}/${String(msg.total_subtasks)} retries=${String(msg.total_retries)} duration=${String(msg.duration_seconds)}s${completionSummary ? ` summary=${completionSummary.slice(0, 180)}` : ''}`);

            if (success) {
                // P3-2: If preview URL is already known, switch immediately
                if (runPreviewUrlRef.current) {
                    setCanvasView('preview');
                    previewReadyForRunRef.current = true;
                }
                clearPreviewFallbackTimer();
                previewFallbackTimerRef.current = setTimeout(() => {
                    if (previewReadyForRunRef.current) return;
                    const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
                    void (async () => {
                        try {
                            const resp = await fetch(`${apiBase}/api/status`);
                            if (!resp.ok) return;
                            const status = await resp.json();
                            const latest = status.latest_artifact as string;
                            const latestMtime = Number(status.latest_artifact_mtime || 0) * 1000;
                            const freshEnough = latestMtime >= (runStartedAtRef.current - 2000);
                            if (latest && freshEnough) {
                                const url = `${apiBase}/preview/${latest}`;
                                setPreviewUrl(withCacheBust(url));
                                setCanvasView('preview');
                                previewReadyForRunRef.current = true;
                                addMessage('system', tr(`已自动检测预览：<a href="${url}" target="_blank">打开</a>`, `Auto-detected preview: <a href="${url}" target="_blank">Open</a>`), 'Preview', '🔗', 'var(--green)');
                                addConsoleLog(`[preview_fallback] accepted latest=${latest} mtime=${String(latestMtime)}`);
                            } else if (latest && !freshEnough) {
                                addMessage('system', tr('检测到的预览文件较旧，已忽略，避免误打开历史产物。', 'Detected preview artifact is stale, ignored to avoid opening old output.'), 'Preview', '⚠️', 'var(--orange)');
                                addConsoleLog(`[preview_fallback] ignored stale latest=${latest} mtime=${String(latestMtime)} runStarted=${String(runStartedAtRef.current)}`);
                            }
                        } catch { /* ignore */ }
                    })();
                }, 3000);
            }

        } else if (t === 'orchestrator_error') {
            setRunning(false);
            addMessage('system', tr(`错误：${msg.error}`, `Error: ${msg.error}`), 'Error', '❌', 'var(--red)');
            addConsoleLog(`[orchestrator_error] ${String(msg.error || '').slice(0, 220)}`);

        } else if (t === 'planning_fallback') {
            const fallbackMsg = String(msg.message || 'Planning failed; switched to fallback plan.');
            addMessage('system', tr(`${fallbackMsg}`, `${fallbackMsg}`), 'Planner', '⚠️', 'var(--orange)');
            addConsoleLog(`[planning_fallback] ${String(msg.reason || msg.message || '').slice(0, 220)}`);

        } else if (t === 'node_start') {
            const nodeId = msg.node_id as string;
            if (nodeId) updateNodeData(nodeId, { status: 'running', progress: 0 });

        } else if (t === 'node_complete') {
            const nodeId = msg.node_id as string;
            if (nodeId) updateNodeData(nodeId, { status: msg.success ? 'done' : 'error', progress: 100 });

        } else if (t === 'config_updated') {
            const providers = (msg.providers as string[]) || [];
            if (providers.length > 0) {
                addMessage('system', tr(`API 密钥已生效（${msg.keys_applied} 个）`, `API keys applied (${msg.keys_applied})`), 'Config', '⚙️', 'var(--green)');
            }

        } else if (t === 'subtask_progress') {
            const stage = String(msg.stage || '');
            const sid = String(msg.subtask_id || '');
            if (stage === 'builder_write' || stage === 'artifact_write') {
                const writtenPath = String(msg.path || '').trim();
                const artifactSync = (msg.artifact_sync as Record<string, unknown> | undefined) || undefined;
                const writer = String(msg.writer || msg.agent || (stage === 'builder_write' ? 'builder' : '')).trim();
                const writerLabel = writer
                    ? writer.charAt(0).toUpperCase() + writer.slice(1)
                    : 'Writer';
                const codeLines = Number(msg.code_lines || 0);
                const totalLines = Number(msg.total_lines || 0);
                const codeKb = Number(msg.code_kb || 0);
                const languages = Array.isArray(msg.languages) ? (msg.languages as string[]) : [];
                if (sid) {
                    appendSubtaskTimeline(
                        sid,
                        writtenPath
                            ? tr(`${writerLabel} 已写入文件：${writtenPath.split('/').pop()}`, `${writerLabel} wrote file: ${writtenPath.split('/').pop()}`)
                            : tr(`${writerLabel} 已写入真实文件。`, `${writerLabel} wrote a real file.`),
                    );
                    const canvasNodeId = subtaskNodeMap.current[sid];
                    if (canvasNodeId && codeLines > 0) {
                        updateNodeData(canvasNodeId, {
                            codeLines,
                            totalLines,
                            codeKb,
                            codeLanguages: languages,
                        });
                    }
                }
                addConsoleLog(`[progress] #${sid} stage=${stage} writer=${writer || 'builder'} path=${writtenPath}${codeLines > 0 ? ` code_lines=${codeLines} code_kb=${codeKb}` : ''}`);
                emitWorkspaceUpdated({
                    eventType: 'subtask_progress',
                    stage,
                    outputDir: String(msg.output_dir || ''),
                    files: writtenPath ? [writtenPath] : [],
                    targetDir: String(artifactSync?.target_dir || ''),
                    copiedFiles: Number(artifactSync?.copied_files || 0),
                    live: Boolean(artifactSync?.live),
                });
            } else if (stage === 'stream_stats') {
                const totalStreamSec = Number(msg.total_stream_sec || 0);
                const charsPerSec = Number(msg.chars_per_sec || 0);
                const firstContentSec = msg.first_content_sec != null ? Number(msg.first_content_sec) : null;
                const modelLatencyMs = totalStreamSec > 0 ? Math.round(totalStreamSec * 1000) : 0;
                if (sid) {
                    const canvasNodeId = subtaskNodeMap.current[sid];
                    if (canvasNodeId && modelLatencyMs > 0) {
                        updateNodeData(canvasNodeId, {
                            modelLatencyMs,
                            charsPerSec,
                            ...(firstContentSec != null ? { firstContentSec } : {}),
                        });
                    }
                }
                addConsoleLog(`[stream_stats] #${sid} model=${msg.model || '?'} total=${totalStreamSec.toFixed(1)}s chars/s=${charsPerSec} chunks=${msg.chunks || 0}`);
            } else if (stage === 'error' && msg.message) {
                if (sid) appendSubtaskTimeline(sid, `执行异常：${String(msg.message).slice(0, 180)}`);
                addMessage('system', `${msg.message}`, 'Error', '⚠️', 'var(--orange)');
                addConsoleLog(`[progress] #${sid} stage=error msg=${String(msg.message).slice(0, 220)}`);
            } else if (stage === 'qa_session_requested') {
                const previewUrlForQa = String(msg.preview_url || msg.previewUrl || '').trim();
                const runId = String(msg.run_id || msg.runId || canonicalRunIdRef.current || '').trim();
                const nodeExecutionId = String(msg.node_execution_id || msg.nodeExecutionId || '').trim();
                const sessionId = String(msg.session_id || msg.sessionId || `${nodeExecutionId}:${sid}` || '').trim();
                const desktopApi = (typeof window !== 'undefined'
                    ? (window as Window & {
                        evermind?: {
                            qa?: {
                                runSession?: (config: Record<string, unknown>) => Promise<DesktopQaSessionResult>;
                            };
                        };
                    }).evermind
                    : undefined);
                const runDesktopQaSession = desktopApi?.qa?.runSession;

                if (previewUrlForQa) {
                    setPreviewUrl(withCacheBust(previewUrlForQa));
                    setCanvasView('preview');
                }

                if (!runDesktopQaSession || !runId || !nodeExecutionId || !sessionId) {
                    if (sid) {
                        appendSubtaskTimeline(
                            sid,
                            tr('桌面 QA 会话不可用，后端将回退到浏览器链路。', 'Desktop QA session unavailable; backend will fall back to the browser path.'),
                        );
                    }
                    addConsoleLog(`[qa_session_requested] skipped session=${sessionId || 'n/a'} run=${runId || 'n/a'} node=${nodeExecutionId || 'n/a'}`);
                } else if (!desktopQaSessionRef.current[sessionId]) {
                    desktopQaSessionRef.current[sessionId] = true;
                    if (sid) {
                        appendSubtaskTimeline(
                            sid,
                            tr('已切到桌面内部 QA 预览会话，正在录屏并采集交互证据。', 'Switched to the internal desktop QA session; recording gameplay evidence now.'),
                        );
                    }
                    addConsoleLog(`[qa_session_requested] session=${sessionId} run=${runId} node=${nodeExecutionId} preview=${previewUrlForQa}`);
                    void (async () => {
                        try {
                            const result = await runDesktopQaSession({
                                ...msg,
                                previewUrl: previewUrlForQa,
                                runId,
                                nodeExecutionId,
                                sessionId,
                                keepOpenMs: 12000,
                                alwaysOnTop: true,
                            });
                            const resultSessionId = String(result.sessionId || result.session_id || sessionId).trim();
                            const artifactMetaBase = {
                                source: 'desktop_qa_session',
                                session_id: resultSessionId,
                                scenario: String(msg.scenario || ''),
                                agent: String(msg.agent || ''),
                                preview_url: previewUrlForQa,
                                ok: Boolean(result.ok),
                                status: String(result.status || ''),
                            };
                            const frames = Array.isArray(result.frames) ? result.frames : [];
                            for (const frame of frames) {
                                const framePath = String(frame.path || '').trim();
                                if (!framePath) continue;
                                await saveArtifact({
                                    run_id: runId,
                                    node_execution_id: nodeExecutionId,
                                    artifact_type: 'qa_session_capture',
                                    title: `QA frame ${Number(frame.index ?? 0) + 1}`,
                                    path: framePath,
                                    metadata: {
                                        ...artifactMetaBase,
                                        frame_index: Number(frame.index ?? 0),
                                        frame_label: String(frame.label || ''),
                                        state_hash: String(frame.stateHash || frame.state_hash || ''),
                                    },
                                });
                            }
                            const videoPath = String(result.videoPath || result.video_path || '').trim();
                            if (videoPath) {
                                await saveArtifact({
                                    run_id: runId,
                                    node_execution_id: nodeExecutionId,
                                    artifact_type: 'qa_session_video',
                                    title: 'QA gameplay recording',
                                    path: videoPath,
                                    metadata: {
                                        ...artifactMetaBase,
                                        frame_count: frames.length,
                                    },
                                });
                            }
                            const timelapsePath = String(result.timelapsePath || result.timelapse_path || '').trim();
                            if (timelapsePath) {
                                await saveArtifact({
                                    run_id: runId,
                                    node_execution_id: nodeExecutionId,
                                    artifact_type: 'qa_session_video',
                                    title: 'QA timelapse recording (500ms/frame)',
                                    path: timelapsePath,
                                    metadata: {
                                        ...artifactMetaBase,
                                        frame_count: Number(result.timelapseFrameCount || result.timelapse_frame_count || 0),
                                        recording_type: 'timelapse',
                                    },
                                });
                            }
                            const rrwebPath = String(result.rrwebRecordingPath || result.rrweb_recording_path || '').trim();
                            const rrwebEventCount = Number(result.rrwebEventCount || result.rrweb_event_count || 0);
                            if (rrwebPath && rrwebEventCount > 0) {
                                await saveArtifact({
                                    run_id: runId,
                                    node_execution_id: nodeExecutionId,
                                    artifact_type: 'qa_session_log',
                                    title: `QA rrweb DOM recording (${rrwebEventCount} events)`,
                                    path: rrwebPath,
                                    metadata: {
                                        ...artifactMetaBase,
                                        recording_type: 'rrweb',
                                        rrweb_event_count: rrwebEventCount,
                                    },
                                });
                            }
                            await saveArtifact({
                                run_id: runId,
                                node_execution_id: nodeExecutionId,
                                artifact_type: 'qa_session_log',
                                title: 'QA session log',
                                content: JSON.stringify(result),
                                metadata: {
                                    ...artifactMetaBase,
                                    frame_count: frames.length,
                                    video_path: videoPath,
                                    timelapse_path: timelapsePath,
                                    rrweb_path: rrwebPath,
                                    rrweb_event_count: rrwebEventCount,
                                    timelapse_frame_count: Number(result.timelapseFrameCount || result.timelapse_frame_count || 0),
                                    log_path: String(result.logPath || result.log_path || '').trim(),
                                },
                            });
                            if (sid) {
                                appendSubtaskTimeline(
                                    sid,
                                    Boolean(result.ok)
                                        ? tr(
                                            `桌面 QA 会话已完成，证据已回传。${rrwebEventCount > 0 ? `（含 rrweb DOM 录屏 ${rrwebEventCount} 事件）` : ''}`,
                                            `Desktop QA session finished and evidence was uploaded.${rrwebEventCount > 0 ? ` (includes rrweb DOM recording with ${rrwebEventCount} events)` : ''}`,
                                        )
                                        : tr('桌面 QA 会话已结束，但证据显示存在异常，后端会按规则决定是否回退。', 'Desktop QA session ended with issues; backend will decide whether to fall back.'),
                                );
                            }
                            addConsoleLog(`[qa_session_complete] session=${resultSessionId} ok=${String(Boolean(result.ok))} frames=${String(frames.length)} video=${videoPath ? '1' : '0'} timelapse=${timelapsePath ? '1' : '0'} rrweb=${String(rrwebEventCount)}`);
                        } catch (error) {
                            if (sid) {
                                appendSubtaskTimeline(
                                    sid,
                                    tr('桌面 QA 会话执行失败，后端将回退到浏览器链路。', 'Desktop QA session failed; backend will fall back to the browser path.'),
                                );
                            }
                            addConsoleLog(`[qa_session_failed] session=${sessionId} error=${String(error).slice(0, 220)}`);
                        } finally {
                            delete desktopQaSessionRef.current[sessionId];
                        }
                    })();
                }
            } else if (stage === 'preview_validation') {
                const ok = Boolean(msg.ok);
                const score = msg.score as number | undefined;
                const errors = (msg.errors as string[]) || [];
                if (sid) appendSubtaskTimeline(sid, ok
                    ? `产物验收通过${typeof score === 'number' ? `（score ${score}）` : ''}`
                    : `产物验收失败：${errors.slice(0, 2).join('；') || '规则校验未通过'}`);
                const canvasNodeId = subtaskNodeMap.current[sid];
                if (canvasNodeId) {
                    appendCanvasNodeLog(canvasNodeId, {
                        ts: Date.now(),
                        msg: ok
                            ? tr(`预览验收通过${typeof score === 'number' ? `（score ${score}）` : ''}`, `Preview validation passed${typeof score === 'number' ? ` (score ${score})` : ''}`)
                            : tr(`预览验收失败：${errors.slice(0, 2).join('；') || '规则校验未通过'}`, `Preview validation failed: ${errors.slice(0, 2).join('; ') || 'rule check failed'}`),
                        type: ok ? 'ok' : 'error',
                    });
                }
                if (ok) addMessage('system', tr(`产物验收通过${score ? `（score ${score}）` : ''}`, `Artifact validation passed${score ? ` (score ${score})` : ''}`), 'Preview Gate', '✅', 'var(--green)');
                else addMessage('system', tr(`产物验收失败：${errors.slice(0, 2).join('；')}`, `Artifact validation failed: ${errors.slice(0, 2).join('; ')}`), 'Preview Gate', '❌', 'var(--red)');
                addConsoleLog(`[progress] #${sid} stage=preview_validation ok=${String(ok)} score=${String(score ?? '')}`);
            } else if (stage === 'preview_validation_failed' && msg.message) {
                if (sid) appendSubtaskTimeline(sid, `预览校验失败：${String(msg.message).slice(0, 180)}`);
                addMessage('system', `${msg.message}`, 'Preview Gate', '❌', 'var(--red)');
                addConsoleLog(`[progress] #${sid} stage=preview_validation_failed msg=${String(msg.message).slice(0, 220)}`);
            } else if (stage === 'preview_waiting_parts') {
                if (sid) appendSubtaskTimeline(sid, tr('并行构建片段已写出，正在等待另一位构建者完成后再组装与校验。', 'Parallel builder fragment saved; waiting for sibling builder before assembly and validation.'));
                addConsoleLog(`[progress] #${sid} stage=preview_waiting_parts`);
            } else if (stage === 'builder_tool_results') {
                const writeCalls = Number(msg.write_calls || 0);
                const count = Number(msg.count || 0);
                if (sid) appendSubtaskTimeline(sid, tr(`工具执行回传 ${count} 条结果，其中检测到 ${writeCalls} 次写文件。`, `Tool loop returned ${count} results, including ${writeCalls} file write operations.`));
                addConsoleLog(`[progress] #${sid} stage=builder_tool_results count=${count} write_calls=${writeCalls}`);
            } else if (stage === 'quality_gate') {
                const score = Number(msg.score || 0);
                const errors = (msg.errors as string[]) || [];
                const warnings = (msg.warnings as string[]) || [];
                if (sid) {
                    appendSubtaskTimeline(sid, score > 0
                        ? tr(`质量门评分 ${score}。${errors.length ? `错误：${errors.slice(0, 2).join('；')}。` : ''}${warnings.length ? `警告：${warnings.slice(0, 2).join('；')}。` : ''}`,
                            `Quality gate score ${score}.${errors.length ? ` Errors: ${errors.slice(0, 2).join('; ')}.` : ''}${warnings.length ? ` Warnings: ${warnings.slice(0, 2).join('; ')}.` : ''}`)
                        : tr('质量门已执行。', 'Quality gate evaluated.'));
                }
                addConsoleLog(`[progress] #${sid} stage=quality_gate score=${score}`);
            } else if (stage === 'quality_gate_failed') {
                if (sid) appendSubtaskTimeline(sid, `质量门失败：${String(msg.message || '').slice(0, 220)}`);
                addConsoleLog(`[progress] #${sid} stage=quality_gate_failed msg=${String(msg.message || '').slice(0, 220)}`);
            } else if (stage === 'reviewer_visual_gate') {
                const ok = Boolean(msg.ok);
                const smokeStatus = String(msg.smoke_status || '');
                const previewUrl = String(msg.preview_url || '');
                const errors = (msg.errors as string[]) || [];
                const warnings = (msg.warnings as string[]) || [];
                const visualStatus = String(msg.visual_status || '');
                const visualSummary = String(msg.visual_summary || '').trim();
                const gateSummary = ok
                    ? tr(
                        `审查员确定性验收通过。${smokeStatus ? ` smoke=${smokeStatus}。` : ''}${visualStatus && visualStatus !== 'skipped' ? ` 视觉回归=${visualStatus}。` : ''}${visualSummary ? ` ${visualSummary}` : ''}${previewUrl ? ` 预览：${previewUrl}` : ''}`,
                        `Reviewer deterministic gate passed.${smokeStatus ? ` smoke=${smokeStatus}.` : ''}${visualStatus && visualStatus !== 'skipped' ? ` visual=${visualStatus}.` : ''}${visualSummary ? ` ${visualSummary}` : ''}${previewUrl ? ` Preview: ${previewUrl}` : ''}`,
                    )
                    : tr(
                        `审查员确定性验收失败：${errors.slice(0, 2).join('；') || '规则校验未通过'}${visualSummary ? `。${visualSummary}` : ''}${warnings.length ? `。警告：${warnings.slice(0, 2).join('；')}` : ''}`,
                        `Reviewer deterministic gate failed: ${errors.slice(0, 2).join('; ') || 'rule check failed'}${visualSummary ? `. ${visualSummary}` : ''}${warnings.length ? `. Warnings: ${warnings.slice(0, 2).join('; ')}` : ''}`,
                    );
                if (sid) appendSubtaskTimeline(sid, gateSummary);
                const canvasNodeId = subtaskNodeMap.current[sid];
                if (canvasNodeId) {
                    appendCanvasNodeLog(canvasNodeId, {
                        ts: Date.now(),
                        msg: gateSummary,
                        type: ok ? (visualStatus === 'warn' ? 'warn' : 'ok') : 'error',
                    });
                }
                addConsoleLog(`[progress] #${sid} stage=reviewer_visual_gate ok=${String(ok)} smoke=${smokeStatus} visual=${visualStatus}`);
            } else if (stage === 'reviewer_forced_rejection') {
                const errors = (msg.errors as string[]) || [];
                const interactionError = String(msg.interaction_error || '').trim();
                const forcedSummary = tr(
                    `审查门已自动转为结构化驳回，Builder 将按整改清单返工。${errors.length ? ` 关键问题：${errors.slice(0, 2).join('；')}。` : ''}${interactionError ? ` 交互门失败：${interactionError}` : ''}`,
                    `Reviewer gate converted the result into a structured rejection so builders can rework against a concrete brief.${errors.length ? ` Key issues: ${errors.slice(0, 2).join('; ')}.` : ''}${interactionError ? ` Interaction gate failed: ${interactionError}` : ''}`,
                );
                if (sid) appendSubtaskTimeline(sid, forcedSummary);
                const canvasNodeId = subtaskNodeMap.current[sid];
                if (canvasNodeId) {
                    appendCanvasNodeLog(canvasNodeId, {
                        ts: Date.now(),
                        msg: forcedSummary,
                        type: 'warn',
                    });
                }
                addConsoleLog(`[progress] #${sid} stage=reviewer_forced_rejection errors=${errors.length}`);
            } else if (stage === 'reviewer_visual_gate_failed' || stage === 'reviewer_interaction_gate_failed' || stage === 'tester_visual_gate_failed' || stage === 'tester_interaction_gate_failed') {
                if (sid) appendSubtaskTimeline(sid, String(msg.message || '').slice(0, 220));
                addConsoleLog(`[progress] #${sid} stage=${stage} msg=${String(msg.message || '').slice(0, 220)}`);
            } else if (stage === 'reviewer_rejection') {
                const rejectionRound = String(msg.rejection_round || '');
                const maxRejections = String(msg.max_rejections || '');
                if (sid) appendSubtaskTimeline(sid, tr(`审查员打回作品（第 ${rejectionRound}/${maxRejections} 轮），已要求构建者按整改清单返工。`, `Reviewer rejected the output (round ${rejectionRound}/${maxRejections}); builders must rework against the remediation brief.`));
                addConsoleLog(`[progress] #${sid} stage=reviewer_rejection round=${rejectionRound}/${maxRejections}`);
            } else if (stage === 'reviewer_rejection_no_retry') {
                if (sid) appendSubtaskTimeline(sid, String(msg.message || '').slice(0, 220));
                addConsoleLog(`[progress] #${sid} stage=reviewer_rejection_no_retry msg=${String(msg.message || '').slice(0, 220)}`);
            } else if (stage === 'analyst_reference_gate_failed') {
                const visited = Array.isArray(msg.visited_urls) ? (msg.visited_urls as string[]) : [];
                const missing = Array.isArray(msg.missing_sections) ? (msg.missing_sections as string[]) : [];
                if (sid) appendSubtaskTimeline(sid, tr(
                    `分析师交付不完整：${String(msg.message || '').slice(0, 220)}${visited.length ? ` 已访问：${visited.slice(0, 3).join('，')}。` : ''}${missing.length ? ` 缺失区块：${missing.join('，')}。` : ''}`,
                    `Analyst handoff incomplete: ${String(msg.message || '').slice(0, 220)}${visited.length ? ` Visited: ${visited.slice(0, 3).join(', ')}.` : ''}${missing.length ? ` Missing sections: ${missing.join(', ')}.` : ''}`));
                addConsoleLog(`[progress] #${sid} stage=analyst_reference_gate_failed visited=${visited.length} missing=${missing.join(',')}`);
            } else if (stage === 'tester_visual_gate') {
                const ok = Boolean(msg.ok);
                const smokeStatus = String(msg.smoke_status || '');
                const previewUrl = String(msg.preview_url || '');
                const errors = (msg.errors as string[]) || [];
                const warnings = (msg.warnings as string[]) || [];
                const visualStatus = String(msg.visual_status || '');
                const visualSummary = String(msg.visual_summary || '').trim();
                if (sid) appendSubtaskTimeline(sid, ok
                    ? tr(
                        `测试员确定性验收通过。${smokeStatus ? ` smoke=${smokeStatus}。` : ''}${visualStatus && visualStatus !== 'skipped' ? ` 视觉回归=${visualStatus}。` : ''}${visualSummary ? ` ${visualSummary}` : ''}${previewUrl ? ` 预览：${previewUrl}` : ''}`,
                        `Tester deterministic gate passed.${smokeStatus ? ` smoke=${smokeStatus}.` : ''}${visualStatus && visualStatus !== 'skipped' ? ` visual=${visualStatus}.` : ''}${visualSummary ? ` ${visualSummary}` : ''}${previewUrl ? ` Preview: ${previewUrl}` : ''}`,
                    )
                    : tr(
                        `测试员确定性验收失败：${errors.slice(0, 2).join('；') || '规则校验未通过'}${visualSummary ? `。${visualSummary}` : ''}${warnings.length ? `。警告：${warnings.slice(0, 2).join('；')}` : ''}`,
                        `Tester deterministic gate failed: ${errors.slice(0, 2).join('; ') || 'rule check failed'}${visualSummary ? `. ${visualSummary}` : ''}${warnings.length ? `. Warnings: ${warnings.slice(0, 2).join('; ')}` : ''}`,
                    ));
                const canvasNodeId = subtaskNodeMap.current[sid];
                if (canvasNodeId) {
                    appendCanvasNodeLog(canvasNodeId, {
                        ts: Date.now(),
                        msg: ok
                            ? tr(
                                `测试员确定性验收通过。${visualSummary || '视觉基线稳定。'}`,
                                `Tester deterministic gate passed. ${visualSummary || 'Visual baseline stayed stable.'}`,
                            )
                            : tr(
                                `测试员确定性验收失败：${errors.slice(0, 2).join('；') || '规则校验未通过'}${visualSummary ? `。${visualSummary}` : ''}`,
                                `Tester deterministic gate failed: ${errors.slice(0, 2).join('; ') || 'rule check failed'}${visualSummary ? `. ${visualSummary}` : ''}`,
                            ),
                        type: ok ? (visualStatus === 'warn' ? 'warn' : 'ok') : 'error',
                    });
                }
                addConsoleLog(`[progress] #${sid} stage=tester_visual_gate ok=${String(ok)} smoke=${smokeStatus} visual=${visualStatus}`);
            } else if (stage === 'requeue_downstream') {
                if (sid) appendSubtaskTimeline(sid, String(msg.message || '').slice(0, 220));
                const requeueSubtasks = Array.isArray(msg.requeue_subtasks)
                    ? msg.requeue_subtasks.map(String)
                    : Array.isArray(msg.requeueSubtasks)
                        ? msg.requeueSubtasks.map(String)
                        : [];
                for (const resetId of requeueSubtasks) {
                    if (!resetId) continue;
                    const canvasNodeId = subtaskNodeMap.current[resetId]
                        || nodes.find((node) => {
                            const data = (node.data || {}) as Record<string, unknown>;
                            return String(data.subtaskId || '').trim() === resetId
                                || String(data.nodeExecutionId || '').trim() === resetId;
                        })?.id
                        || '';
                    const prevRuntime = runSubtasksRef.current[resetId] || {};
                    runSubtasksRef.current[resetId] = {
                        ...prevRuntime,
                        status: 'queued',
                        error: '',
                        output: '',
                        startedAt: 0,
                        endedAt: 0,
                        durationSeconds: 0,
                    };
                    appendSubtaskTimeline(resetId, tr('审查退回，节点已重置并等待重新执行。', 'Reviewer requested rework; node reset and queued again.'));
                    if (canvasNodeId) {
                        updateNodeData(canvasNodeId, {
                            status: 'queued',
                            progress: 0,
                            phase: 'requeued',
                            startedAt: 0,
                            endedAt: 0,
                            durationSeconds: 0,
                            outputSummary: '',
                            lastOutput: '',
                            error: '',
                            _terminalStatus: false,
                        });
                    }
                }
                addConsoleLog(`[progress] #${sid} stage=requeue_downstream msg=${String(msg.message || '').slice(0, 220)}`);
            } else if (stage === 'model_downgrade') {
                const fromModel = String(msg.from_model || '');
                const toModel = String(msg.to_model || '');
                if (sid) appendSubtaskTimeline(sid, `模型降级重试：${fromModel} → ${toModel}`);
                addMessage('system', tr(`模型降级重试：<code>${fromModel}</code> → <code>${toModel}</code>`, `Model downgrade retry: <code>${fromModel}</code> → <code>${toModel}</code>`), 'Auto Recovery', '🔄', 'var(--orange)');
                addConsoleLog(`[progress] #${sid} stage=model_downgrade ${fromModel} -> ${toModel}`);
            } else if (stage === 'skills_loaded') {
                const skills = (msg.skills as string[]) || [];
                if (skills.length > 0 && sid) {
                    const canvasNodeId = subtaskNodeMap.current[sid];
                    if (canvasNodeId) {
                        updateNodeData(canvasNodeId, { loadedSkills: skills });
                        appendCanvasNodeLog(canvasNodeId, {
                            ts: Date.now(),
                            msg: lang === 'zh'
                                ? `已加载技能：${skills.join(', ')}`
                                : `Loaded skills: ${skills.join(', ')}`,
                            type: 'sys',
                        });
                    }
                    appendSubtaskTimeline(sid, `已加载 ${skills.length} 个技能：${skills.slice(0, 3).join(', ')}${skills.length > 3 ? '...' : ''}`);
                    addConsoleLog(`[progress] #${sid} stage=skills_loaded skills=[${skills.join(',')}]`);
                }
            } else if (stage === 'waiting_ai') {
                const agent = String(msg.agent || 'agent');
                const elapsed = Number(msg.elapsed_sec || 0);
                // §FIX: Throttle waiting_ai console logs to one entry every 30s per node.
                const lastLoggedElapsed = waitingAiLastConsoleLogRef.current[sid] || 0;
                if (!lastLoggedElapsed || elapsed - lastLoggedElapsed >= 30) {
                    waitingAiLastConsoleLogRef.current[sid] = elapsed;
                    addConsoleLog(`[progress] #${sid} stage=waiting_ai agent=${agent} elapsed=${elapsed}s`);
                }
                // §PROGRESS-FIX: Update canvas node with live progress during execution
                const canvasNodeId = subtaskNodeMap.current[sid];
                if (canvasNodeId) {
                    // §F2-2: Skip heartbeat progress updates for nodes that already reached terminal status
                    const existingNode = nodes.find((node) => node.id === canvasNodeId);
                    const existingData = (existingNode?.data || {}) as Record<string, unknown>;
                    const terminalStatuses = ['passed', 'failed', 'blocked', 'completed', 'done', 'error'];
                    if (existingData._terminalStatus || terminalStatuses.includes(String(existingData.status || ''))) {
                        // Node already completed — do NOT overwrite progress or status from heartbeat
                    } else {
                        // Smooth progress: 5 → 90 over agent-specific timeout period
                        const agentTimeouts: Record<string, number> = { builder: 960, analyst: 540, reviewer: 480, planner: 120, tester: 480, deployer: 300, debugger: 600 };
                        const agentTimeout = agentTimeouts[agent.toLowerCase()] || 360;
                        const progressPct = Math.min(90, Math.round(5 + (elapsed / agentTimeout) * 85));
                        const rawMsg = String(msg.partial_output || msg.message || '');
                        const incomingSkills = Array.isArray(msg.loaded_skills) ? (msg.loaded_skills as string[]) : [];
                        const existingSkills = Array.isArray(existingData.loadedSkills) ? (existingData.loadedSkills as string[]) : [];
                        const mergedSkills = existingSkills.length > 0 ? existingSkills : incomingSkills;
                        const humanActivity = buildReadableCurrentWork({
                            lang,
                            nodeType: String(existingData.nodeType || agent || 'builder'),
                            status: 'running',
                            phase: String(existingData.phase || ''),
                            taskDescription: String(existingData.taskDescription || ''),
                            loadedSkills: mergedSkills,
                            outputSummary: rawMsg,
                            lastOutput: rawMsg,
                            logs: Array.isArray(existingData.log) ? (existingData.log as Array<{ ts?: number; msg?: string; type?: string }>) : [],
                            durationText: `${elapsed}s`,
                        });
                        // Live duration update: calculate from startedAt so the timer never freezes
                        const nodeStartedAt = Number(existingData.startedAt) || runStartedAtRef.current;
                        const liveDuration = Math.max(0, Math.round((Date.now() - nodeStartedAt) / 1000));
                        updateNodeData(canvasNodeId, {
                            progress: progressPct,
                            status: 'running',
                            outputSummary: humanActivity,
                            durationSeconds: liveDuration,
                            ...(mergedSkills.length > 0 && existingSkills.length === 0 ? { loadedSkills: mergedSkills } : {}),
                        });
                    }
                }
                const nowMs = Date.now();
                const last = waitingAiLastNotifyRef.current[sid] || 0;
                if (!last || nowMs - last >= 60000) {
                    waitingAiLastNotifyRef.current[sid] = nowMs;
                    addMessage('system', tr(`${agent} #${sid} 仍在执行中，请稍候...`, `${agent} #${sid} is still running, please wait...`), `${agent} #${sid}`, '⏳', 'var(--orange)');
                }
            } else if (stage === 'partial_output') {
                // Real-time AI output: forward model's actual response text to canvas node
                const preview = String(msg.preview || msg.partial_output || '').trim();
                const source = String(msg.source || '').toLowerCase();
                if (preview && source === 'model') {
                    const canvasNodeId = subtaskNodeMap.current[sid];
                    if (canvasNodeId) {
                        const existingNode = nodes.find((node) => node.id === canvasNodeId);
                        const existingData = (existingNode?.data || {}) as Record<string, unknown>;
                        const phase = String(msg.phase || existingData.phase || 'drafting');
                        // v3.0: Extract code metrics for enhanced progress display
                        const codeLines = Number(msg.code_lines || 0);
                        const totalLines = Number(msg.total_lines || 0);
                        const codeKb = Number(msg.code_kb || 0);
                        const languages = Array.isArray(msg.languages) ? (msg.languages as string[]) : [];
                        const humanActivity = buildReadableCurrentWork({
                            lang,
                            nodeType: String(existingData.nodeType || 'builder'),
                            status: 'running',
                            phase,
                            taskDescription: String(existingData.taskDescription || ''),
                            loadedSkills: Array.isArray(existingData.loadedSkills) ? (existingData.loadedSkills as string[]) : [],
                            outputSummary: '',
                            lastOutput: preview,
                            logs: Array.isArray(existingData.log) ? (existingData.log as Array<{ ts?: number; msg?: string; type?: string }>) : [],
                        });
                        // v3.0: Build code metrics text for node display
                        const codeMetricsText = codeLines > 0
                            ? (lang === 'zh'
                                ? `已输出 ${codeLines} 行代码 / ${codeKb}KB${languages.length ? ` (${languages.join('+')})` : ''}`
                                : `${codeLines} lines / ${codeKb}KB${languages.length ? ` (${languages.join('+')})` : ''}`)
                            : '';
                        updateNodeData(canvasNodeId, {
                            outputSummary: codeMetricsText ? `${codeMetricsText}\n${humanActivity}` : humanActivity,
                            lastOutput: preview,
                            hasModelPartialOutput: true,
                            phase,
                            // v3.0: Store code metrics for NodeDetailPopup
                            ...(codeLines > 0 ? {
                                codeLines,
                                totalLines,
                                codeKb,
                                codeLanguages: languages,
                            } : {}),
                        });
                    }
                    addConsoleLog(`[progress] #${sid} stage=partial_output source=${source} len=${preview.length}${msg.code_lines ? ` code_lines=${msg.code_lines} code_kb=${msg.code_kb}` : ''}`);
                }
            } else if (stage === 'stream_stalled') {
                const reason = String(msg.reason || '').slice(0, 180);
                addConsoleLog(`[progress] #${sid} stage=stream_stalled reason=${reason}`);
                if (sid) appendSubtaskTimeline(sid, '模型流式输出停滞，已触发快速失败并进入重试。');
                addMessage('system', tr(`${sid ? `#${sid} ` : ''}模型输出停滞，系统将自动快速重试。`, `${sid ? `#${sid} ` : ''}model stream stalled; auto-retrying quickly.`), 'Model', '⚠️', 'var(--orange)');
            } else if (stage === 'builder_loop_guard') {
                const streak = Number(msg.streak || 0);
                const threshold = Number(msg.threshold || 0);
                const reason = String(msg.reason || 'tool_research_loop');
                addConsoleLog(`[progress] #${sid} stage=builder_loop_guard streak=${streak} threshold=${threshold} reason=${reason}`);
                if (sid) appendSubtaskTimeline(sid, `构建者触发循环保护：连续 ${streak} 次工具调用未产出文件，切换为强制文本输出。`);
                addMessage('system', tr(`builder #${sid} 触发循环保护，正在强制输出完整 HTML（原因：${reason}）`, `builder #${sid} loop guard triggered, forcing full HTML output (reason: ${reason})`), `builder #${sid}`, '⚠️', 'var(--orange)');
            } else if (stage === 'browser_action') {
                const action = String(msg.action || 'unknown');
                const subaction = String(msg.subaction || msg.intent || '').trim();
                const ok = Boolean(msg.ok);
                const mode = String(msg.browser_mode || 'unknown');
                const requestedMode = String(msg.requested_mode || '');
                const url = String(msg.url || '');
                const err = String(msg.error || '');
                const launchNote = String(msg.launch_note || '');
                const observation = String(msg.observation || '').trim();
                const target = String(msg.target || '').trim();
                const snapshotRefs = Array.isArray(msg.snapshot_refs_preview)
                    ? (msg.snapshot_refs_preview as Array<Record<string, unknown>>)
                    : [];
                const snapshotRefText = action === 'snapshot' && snapshotRefs.length > 0
                    ? snapshotRefs
                        .slice(0, 4)
                        .map((item) => {
                            const ref = String(item.ref || '').trim();
                            const label = String(item.label || '').trim();
                            const role = String(item.role || '').trim();
                            const bits = [ref, label].filter(Boolean);
                            return role ? `${bits.join(' ')} [${role}]` : bits.join(' ');
                        })
                        .filter(Boolean)
                        .join(', ')
                    : '';
                addConsoleLog(`[browser] #${sid} action=${action} ok=${String(ok)} mode=${mode}${requestedMode ? ` requested=${requestedMode}` : ''}${url ? ` url=${url}` : ''}${launchNote ? ` note=${launchNote.slice(0, 140)}` : ''}${err ? ` error=${err.slice(0, 120)}` : ''}`);
                if (sid) {
                    const actionText = (() => {
                        if (!ok) {
                            return `浏览器步骤：${action}${subaction ? `/${subaction}` : ''} 执行失败（模式 ${mode}）${err ? `，错误：${err.slice(0, 120)}` : ''}`;
                        }
                        if (action === 'observe') {
                            return `浏览器步骤：观察当前页面并整理可交互元素（模式 ${mode}）${snapshotRefText ? `，可交互引用 ${snapshotRefText}` : ''}${observation ? `，摘要：${observation.slice(0, 180)}` : ''}`;
                        }
                        if (action === 'act') {
                            return `浏览器步骤：执行 ${subaction || '交互'}（模式 ${mode}）${target ? `，目标 ${target}` : ''}${url ? `，页面 ${url}` : ''}${snapshotRefText ? `，参考 ${snapshotRefText}` : ''}${observation ? `，摘要：${observation.slice(0, 120)}` : ''}`;
                        }
                        if (action === 'extract') {
                            return `浏览器步骤：提取页面关键信息（模式 ${mode}）${observation ? `，摘要：${observation.slice(0, 180)}` : ''}`;
                        }
                        return `浏览器步骤：${action} 执行成功（模式 ${mode}）${url ? `，目标 ${url}` : ''}${snapshotRefText ? `，可交互引用 ${snapshotRefText}` : ''}`;
                    })();
                    appendSubtaskTimeline(sid, actionText);
                    const canvasNodeId = subtaskNodeMap.current[sid];
                    if (canvasNodeId) {
                        appendCanvasNodeLog(canvasNodeId, {
                            ts: Date.now(),
                            msg: actionText,
                            type: ok ? 'info' : 'error',
                        });
                    }
                }
                if (sid && !browserModeNotifiedRef.current[sid]) {
                    browserModeNotifiedRef.current[sid] = true;
                    const modeText = mode === 'headful' ? tr('可见窗口', 'visible window') : mode === 'headless' ? tr('无头后台', 'headless background') : mode;
                    addMessage('system', tr(`节点 #${sid} 已进入浏览器测试（${modeText}）`, `Node #${sid} entered browser testing (${modeText})`), 'Browser', '🌐', mode === 'headful' ? 'var(--green)' : 'var(--orange)');
                    if (launchNote) addMessage('system', tr(`浏览器模式降级：${launchNote}`, `Browser mode fallback: ${launchNote}`), 'Browser', '⚠️', 'var(--orange)');
                }
            } else if (stage === 'executing_plugin') {
                addConsoleLog(`[progress] #${sid} stage=executing_plugin plugin=${String(msg.plugin || 'unknown')}`);
            } else if (stage) {
                addConsoleLog(`[progress] #${sid} stage=${stage}`);
            }

        } else if (t === 'system_info') {
            addMessage('system', `${msg.message}`, 'System', 'ℹ️', 'var(--blue)');
            addConsoleLog(`[system_info] ${String(msg.message || '').slice(0, 220)}`);

        // ── OpenClaw V1 Connector Messages ──
        } else if (t === 'openclaw_node_ack') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const neId = String(payload.nodeExecutionId || '');
            const canvasNodeId = resolveCanvasNodeId(payload);
            rememberPreviewRunId(payload.runId);
            if (canvasNodeId && neId) {
                updateNodeData(canvasNodeId, {
                    nodeExecutionId: neId,
                    runtime: 'openclaw',
                });
            }
            addConsoleLog(`[openclaw_node_ack] nodeExec=${neId} accepted=${String(payload.accepted)}`);
            const nodeKey = String(payload.nodeKey || payload.nodeLabel || neId).slice(0, 60);
            const accepted = payload.accepted !== false;
            addMessage(
                'system',
                accepted
                    ? (lang === 'zh' ? `已派发节点: ${nodeKey}` : `Dispatched: ${nodeKey}`)
                    : (lang === 'zh' ? `节点派发被拒绝: ${nodeKey}` : `Dispatch rejected: ${nodeKey}`),
                'OpenClaw',
                'OC',
                accepted ? 'var(--blue)' : 'var(--red)',
            );
            emitConnectorEvent(
                'openclaw_node_ack',
                accepted
                    ? (lang === 'zh' ? `节点已接收: ${nodeKey}` : `Node accepted: ${nodeKey}`)
                    : (lang === 'zh' ? `节点被拒绝: ${nodeKey}` : `Node rejected: ${nodeKey}`),
                neId || String(payload.runId || ''),
                toEpochSeconds(msg.timestamp, Date.now() / 1000) * 1000,
            );

        } else if (t === 'evermind_dispatch_node') {
            // P1-2B: Auto-chained dispatch broadcast — update NE status to running
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const neId = String(payload.nodeExecutionId || '');
            const runId = String(payload.runId || '');
            const taskId = String(payload.taskId || '');
            rememberPreviewRunId(payload.runId);
            addConsoleLog(`[evermind_dispatch_node] nodeExec=${neId} nodeKey=${String(payload.nodeKey || '')} autoChained=${String(payload.autoChained || false)}`);
            // B-3: Feed milestone for dispatch events
            const dispatchLabel = String(payload.nodeLabel || payload.nodeKey || neId).slice(0, 60);
            addMessage('system',
                tr(`☁ 已派发节点给 OpenClaw: ${dispatchLabel}`, `☁ Dispatched to OpenClaw: ${dispatchLabel}`),
                'OpenClaw', 'OC', '#a855f7');
            emitConnectorEvent(
                'evermind_dispatch_node',
                tr(`准备派发节点: ${String(payload.nodeLabel || payload.nodeKey || neId).slice(0, 60)}`, `Dispatch queued: ${String(payload.nodeLabel || payload.nodeKey || neId).slice(0, 60)}`),
                runId || taskId || undefined,
                Date.now(),
            );
            if (neId && onMergeNodeExecution) {
                onMergeNodeExecution({
                    id: neId,
                    run_id: runId,
                    status: 'running',
                    updated_at: Date.now() / 1000,
                    ...(typeof payload._neVersion === 'number' ? { version: payload._neVersion } : {}),
                });
            }
            if (runId && neId && onMergeRun) {
                const runPatch: Partial<RunRecord> & Pick<RunRecord, 'id'> = {
                    id: runId,
                    current_node_execution_id: neId,
                    updated_at: Date.now() / 1000,
                    ...(typeof payload._runVersion === 'number' ? { version: payload._runVersion } : {}),
                };
                if (taskId) runPatch.task_id = taskId;
                if (payload.runtime) runPatch.runtime = String(payload.runtime) as RunRecord['runtime'];
                if (payload.workflowTemplateId) runPatch.workflow_template_id = String(payload.workflowTemplateId);
                if (payload.runStatus) runPatch.status = String(payload.runStatus) as RunRecord['status'];
                if (Array.isArray(payload.activeNodeExecutionIds)) {
                    runPatch.active_node_execution_ids = payload.activeNodeExecutionIds.map(String);
                }
                onMergeRun(runPatch);
            }
            if (taskId && onMergeTask) {
                const taskPatch: Partial<TaskCard> & Pick<TaskCard, 'id'> = {
                    id: taskId,
                    updatedAt: Date.now(),
                    ...(typeof payload._taskVersion === 'number' ? { version: payload._taskVersion } : {}),
                };
                if (payload.taskStatus) {
                    taskPatch.status = String(payload.taskStatus) as TaskCard['status'];
                }
                onMergeTask(taskPatch);
            }
            const canvasNodeId = resolveCanvasNodeId(payload);
            if (canvasNodeId) {
                updateNodeData(canvasNodeId, {
                    runtime: String(payload.runtime || 'openclaw'),
                    status: 'running',
                });
            }

        } else if (t === 'evermind_cancel_run') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const runId = String(payload.runId || '');
            const taskId = String(payload.taskId || '');
            const runStatus = String(payload.runStatus || '').trim();
            const taskStatus = String(payload.taskStatus || '').trim();
            const updatedAt = Date.now() / 1000;
            addConsoleLog(`[evermind_cancel_run] runId=${runId} reason=${String(payload.reason || 'manual')}`);
            if (runId && onMergeRun) {
                const runPatch = clearRunActivity({
                    id: runId,
                    status: (runStatus || 'cancelled') as RunRecord['status'],
                    updated_at: updatedAt,
                    ...(typeof payload._runVersion === 'number' ? { version: payload._runVersion } : {}),
                });
                onMergeRun(runPatch);
            }
            if (taskId && onMergeTask) {
                const taskPatch: Partial<TaskCard> & Pick<TaskCard, 'id'> = {
                    id: taskId,
                    updatedAt: updatedAt * 1000,
                    ...(typeof payload._taskVersion === 'number' ? { version: payload._taskVersion } : {}),
                };
                if (taskStatus) {
                    taskPatch.status = taskStatus as TaskCard['status'];
                }
                onMergeTask(taskPatch);
            }

        } else if (t === 'openclaw_node_update') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const neId = String(payload.nodeExecutionId || '');
            const status = String(payload.status || '');
            const updatedAt = toEpochSeconds(payload.timestamp);
            addConsoleLog(`[openclaw_node_update] nodeExec=${neId} status=${status} progress=${String(payload.progress || 0)}`);
            // B-3: feed milestone for running and terminal statuses
            if (status === 'running') {
                const nodeLabel = String(payload.nodeLabel || payload.nodeKey || neId).slice(0, 60);
                addMessage('system',
                    tr(`⚙️ OpenClaw 正在执行: ${nodeLabel}`, `⚙️ OpenClaw executing: ${nodeLabel}`),
                    'OpenClaw', 'OC', 'var(--blue)');
                emitConnectorEvent(
                    'openclaw_node_update',
                    tr(`节点执行中: ${nodeLabel}`, `Node running: ${nodeLabel}`),
                    'running',
                    updatedAt * 1000,
                );
            } else if (['passed', 'failed', 'cancelled', 'skipped'].includes(status)) {
                const nodeLabel = String(payload.nodeLabel || payload.nodeKey || neId).slice(0, 60);
                const humanStatus = connectorNodeStatusLabel(status, lang);
                const statusColor = status === 'passed' || status === 'skipped' ? 'var(--green)' : 'var(--red)';
                addMessage('system', lang === 'zh' ? `节点 ${nodeLabel}: ${humanStatus}` : `Node ${nodeLabel}: ${humanStatus}`, 'OpenClaw', 'OC', statusColor);
                emitConnectorEvent(
                    'openclaw_node_update',
                    lang === 'zh' ? `节点状态更新: ${nodeLabel}` : `Node updated: ${nodeLabel}`,
                    humanStatus,
                    updatedAt * 1000,
                );
            }
            rememberPreviewRunId(payload.runId);
            // Pipe V1 metrics into canvas node data for AgentNode rendering
            const canvasNodeId = resolveCanvasNodeId(payload);
            if (canvasNodeId) {
                const updatePayload: Record<string, unknown> = { status };
                const fallbackProgress = ['passed', 'failed', 'cancelled', 'skipped'].includes(status)
                    ? 100
                    : status === 'running'
                        ? 5
                        : 0;
                const hasPartialOutputSummary = payload.partialOutputSummary !== undefined;
                const hasFinalOutputSummary = payload.outputSummary !== undefined;
                const hasOutputSummaryField = hasPartialOutputSummary || hasFinalOutputSummary;
                const hasStartedAtField = payload.startedAt !== undefined;
                const hasEndedAtField = payload.endedAt !== undefined;
                const hasTimingFields = hasStartedAtField || hasEndedAtField;
                const startedAtMs = normalizeEpochMs(payload.startedAt, 0);
                const endedAtMs = normalizeEpochMs(payload.endedAt, 0);
                const durationSeconds = deriveDurationSeconds(startedAtMs, endedAtMs);
                const partialOutputSummary = hasPartialOutputSummary ? String(payload.partialOutputSummary || '').trim() : '';
                const finalOutputSummary = hasFinalOutputSummary ? String(payload.outputSummary || '').trim() : '';
                const outputSummary = partialOutputSummary || finalOutputSummary;
                const nodeLabel = String(payload.nodeLabel || payload.nodeKey || neId).slice(0, 80);
                const existingNode = nodes.find((node) => node.id === canvasNodeId);
                const existingData = (existingNode?.data || {}) as Record<string, unknown>;
                const phaseValue = String(payload.phase || existingData.phase || '').trim();
                const incomingSkills = Array.isArray(payload.loadedSkills) ? payload.loadedSkills.map(String) : [];
                const existingSkills = Array.isArray(existingData.loadedSkills) ? (existingData.loadedSkills as string[]) : [];
                const mergedSkills = [...new Set([...existingSkills, ...incomingSkills])];
                const humanOutputSummary = outputSummary
                    ? buildReadableCurrentWork({
                        lang,
                        nodeType: String(existingData.nodeType || payload.nodeKey || 'builder'),
                        status,
                        phase: phaseValue,
                        taskDescription: String(payload.inputSummary || existingData.taskDescription || ''),
                        loadedSkills: mergedSkills,
                        outputSummary,
                        lastOutput: outputSummary,
                        logs: Array.isArray(payload.activityLog)
                            ? (payload.activityLog as Array<{ ts?: number; msg?: string; type?: string }>)
                            : (Array.isArray(existingData.log) ? (existingData.log as Array<{ ts?: number; msg?: string; type?: string }>) : []),
                    })
                    : '';
                if (neId) updatePayload.nodeExecutionId = neId;
                if (payload.nodeKey !== undefined) {
                    updatePayload.rawNodeKey = String(payload.nodeKey || '');
                    updatePayload.nodeType = normalizeCanvasNodeType(payload.nodeKey || existingData.nodeType || 'builder');
                }
                updatePayload.runtime = String(payload.runtime || 'openclaw');
                updatePayload.progress = Math.max(0, Math.min(100, payload.progress !== undefined ? Number(payload.progress) : fallbackProgress));
                if (payload.tokensUsed !== undefined) updatePayload.tokensUsed = Number(payload.tokensUsed);
                if (payload.cost !== undefined) updatePayload.cost = Number(payload.cost);
                if (payload.costDelta !== undefined && !payload.cost) updatePayload.cost = Number(payload.costDelta);
                if (payload.assignedModel) updatePayload.assignedModel = String(payload.assignedModel);
                if (payload.inputSummary !== undefined) updatePayload.taskDescription = String(payload.inputSummary || '');
                if (hasOutputSummaryField) {
                    updatePayload.outputSummary = humanOutputSummary || outputSummary;
                    updatePayload.lastOutput = outputSummary;
                }
                if (mergedSkills.length > 0) {
                    updatePayload.loadedSkills = mergedSkills;
                }
                if (phaseValue) updatePayload.phase = phaseValue;
                if (payload.errorMessage !== undefined) updatePayload.error = String(payload.errorMessage || '').trim();
                if (hasStartedAtField) updatePayload.startedAt = startedAtMs;
                if (hasEndedAtField) updatePayload.endedAt = endedAtMs;
                if (hasTimingFields) updatePayload.durationSeconds = durationSeconds ?? 0;
                // v3.0.5: Code output metrics from NE
                if (payload.codeLines !== undefined && Number(payload.codeLines) > 0) {
                    updatePayload.codeLines = Number(payload.codeLines);
                }
                if (payload.totalLines !== undefined && Number(payload.totalLines) > 0) {
                    updatePayload.totalLines = Number(payload.totalLines);
                }
                if (payload.codeKb !== undefined && Number(payload.codeKb) > 0) {
                    updatePayload.codeKb = Number(payload.codeKb);
                }
                if (Array.isArray(payload.codeLanguages) && payload.codeLanguages.length > 0) {
                    updatePayload.codeLanguages = payload.codeLanguages.map(String);
                }
                if (payload.modelLatencyMs !== undefined && Number(payload.modelLatencyMs) > 0) {
                    updatePayload.modelLatencyMs = Number(payload.modelLatencyMs);
                }
                if (status === 'queued' || status === 'running') updatePayload._terminalStatus = false;
                if (['passed', 'failed', 'cancelled', 'skipped'].includes(status)) updatePayload._terminalStatus = true;
                updateNodeData(canvasNodeId, updatePayload);
                if (Array.isArray(payload.activityLog) && payload.activityLog.length > 0) {
                    const normalizedActivityLog = (payload.activityLog as Array<{ ts?: number; msg?: string; type?: string }>)
                        .filter((entry) => typeof entry?.msg === 'string' && String(entry.msg).trim().length > 0)
                        .map((entry) => ({
                            ts: entry.ts,
                            msg: String(entry.msg).trim(),
                            type: entry.type,
                        }));
                    mergeCanvasNodeLogs(
                        canvasNodeId,
                        normalizedActivityLog,
                    );
                }
                if (status) {
                    appendCanvasNodeLog(canvasNodeId, {
                        ts: updatedAt * 1000,
                        msg: lang === 'zh'
                            ? `状态更新：${nodeLabel} -> ${connectorNodeStatusLabel(status, lang)}`
                            : `Status update: ${nodeLabel} -> ${connectorNodeStatusLabel(status, lang)}`,
                        type: status === 'failed' || status === 'cancelled'
                            ? 'error'
                            : status === 'passed' || status === 'skipped'
                                ? 'ok'
                                : 'info',
                    });
                }
                if (outputSummary) {
                    const outputDescriptor = describeNodeActivity(outputSummary, lang, {
                        nodeType: String(existingData.nodeType || payload.nodeKey || 'builder'),
                        status,
                    });
                    if (outputDescriptor && !outputDescriptor.lowSignal) {
                        appendCanvasNodeLog(canvasNodeId, {
                            ts: updatedAt * 1000,
                            msg: outputDescriptor.text,
                            type: outputDescriptor.type,
                        });
                    }
                }
            }
            // ── P0-1: Merge node execution into canonical state ──
            if (neId && onMergeNodeExecution) {
                const nodePatch: Partial<NodeExecutionRecord> & Pick<NodeExecutionRecord, 'id' | 'run_id'> = {
                    id: neId,
                    run_id: String(payload.runId || ''),
                    updated_at: updatedAt,
                };
                if (payload.nodeKey !== undefined) nodePatch.node_key = String(payload.nodeKey || '');
                if (payload.nodeLabel !== undefined) nodePatch.node_label = String(payload.nodeLabel || '');
                if (payload.assignedModel !== undefined) nodePatch.assigned_model = String(payload.assignedModel || '');
                if (payload.assignedProvider !== undefined) nodePatch.assigned_provider = String(payload.assignedProvider || '');
                if (status) nodePatch.status = status as NodeExecutionRecord['status'];
                if (payload.retryCount !== undefined) nodePatch.retry_count = Number(payload.retryCount || 0);
                if (payload.tokensUsed !== undefined) nodePatch.tokens_used = Number(payload.tokensUsed || 0);
                if (payload.cost !== undefined) nodePatch.cost = Number(payload.cost || 0);
                if (payload.inputSummary !== undefined) nodePatch.input_summary = String(payload.inputSummary || '');
                if (payload.partialOutputSummary !== undefined || payload.outputSummary !== undefined) {
                    nodePatch.output_summary = String(payload.partialOutputSummary || payload.outputSummary || '');
                }
                if (Array.isArray(payload.loadedSkills)) nodePatch.loaded_skills = payload.loadedSkills.map(String);
                if (Array.isArray(payload.activityLog)) nodePatch.activity_log = payload.activityLog as NodeExecutionRecord['activity_log'];
                if (Array.isArray(payload.referenceUrls)) nodePatch.reference_urls = payload.referenceUrls.map(String);
                if (payload.currentAction !== undefined) nodePatch.current_action = String(payload.currentAction || '');
                if (Array.isArray(payload.workSummary)) nodePatch.work_summary = payload.workSummary.map(String);
                if (payload.toolCallStats && typeof payload.toolCallStats === 'object') {
                    nodePatch.tool_call_stats = Object.fromEntries(
                        Object.entries(payload.toolCallStats as Record<string, unknown>).map(([key, value]) => [key, Number(value || 0)]),
                    );
                }
                if (Array.isArray(payload.reportArtifactIds)) nodePatch.report_artifact_ids = payload.reportArtifactIds.map(String);
                if (Array.isArray(payload.handoffArtifactIds)) nodePatch.handoff_artifact_ids = payload.handoffArtifactIds.map(String);
                if (payload.dossierArtifactId !== undefined) nodePatch.dossier_artifact_id = String(payload.dossierArtifactId || '');
                if (payload.summaryArtifactId !== undefined) nodePatch.summary_artifact_id = String(payload.summaryArtifactId || '');
                if (payload.blockingReason !== undefined) nodePatch.blocking_reason = String(payload.blockingReason || '');
                if (payload.latestReviewDecision !== undefined) nodePatch.latest_review_decision = String(payload.latestReviewDecision || '');
                if (payload.latestReviewReportArtifactId !== undefined) nodePatch.latest_review_report_artifact_id = String(payload.latestReviewReportArtifactId || '');
                if (payload.latestMergeManifestArtifactId !== undefined) nodePatch.latest_merge_manifest_artifact_id = String(payload.latestMergeManifestArtifactId || '');
                if (payload.latestDeploymentReceiptArtifactId !== undefined) nodePatch.latest_deployment_receipt_artifact_id = String(payload.latestDeploymentReceiptArtifactId || '');
                // v3.0.5: Code output metrics
                if (payload.codeLines !== undefined) nodePatch.code_lines = Number(payload.codeLines || 0);
                if (payload.totalLines !== undefined) nodePatch.total_lines = Number(payload.totalLines || 0);
                if (payload.codeKb !== undefined) nodePatch.code_kb = Number(payload.codeKb || 0);
                if (Array.isArray(payload.codeLanguages)) nodePatch.code_languages = payload.codeLanguages.map(String);
                if (payload.modelLatencyMs !== undefined) nodePatch.model_latency_ms = Number(payload.modelLatencyMs || 0);
                if (payload.errorMessage !== undefined) nodePatch.error_message = String(payload.errorMessage || '');
                if (Array.isArray(payload.artifactIds)) nodePatch.artifact_ids = payload.artifactIds.map(String);
                if (payload.startedAt !== undefined) nodePatch.started_at = toEpochSeconds(payload.startedAt, 0);
                if (payload.endedAt !== undefined) nodePatch.ended_at = toEpochSeconds(payload.endedAt, 0);
                if (payload.createdAt !== undefined) nodePatch.created_at = toEpochSeconds(payload.createdAt, updatedAt);
                // P0-3: pass version from broadcast
                if (typeof payload._neVersion === 'number') nodePatch.version = payload._neVersion;
                onMergeNodeExecution(nodePatch);
            }
            if (onMergeRun && payload.runId && neId) {
                const activeNodeExecutionIds = Array.isArray(payload.activeNodeExecutionIds)
                    ? payload.activeNodeExecutionIds.map(String)
                    : null;
                const runPatch: Partial<RunRecord> & Pick<RunRecord, 'id'> = {
                    id: String(payload.runId),
                    updated_at: updatedAt,
                    // P0-3: pass version from broadcast
                    ...(typeof payload._runVersion === 'number' ? { version: payload._runVersion } : {}),
                };
                if (status === 'running') {
                    runPatch.current_node_execution_id = neId;
                } else if (activeNodeExecutionIds) {
                    runPatch.current_node_execution_id = activeNodeExecutionIds[activeNodeExecutionIds.length - 1] || '';
                }
                if (activeNodeExecutionIds) {
                    runPatch.active_node_execution_ids = activeNodeExecutionIds;
                }
                onMergeRun(runPatch);
            }

        } else if (t === 'openclaw_node_progress') {
            // P2-C: Real-time node progress streaming (partial output, tool calls, %)
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const neId = String(payload.nodeExecutionId || '');
            const runId = String(payload.runId || '');
            const progressPct = payload.progress !== undefined ? Number(payload.progress) : undefined;
            const partialOutput = payload.partialOutput !== undefined ? String(payload.partialOutput) : undefined;
            const toolCall = payload.toolCall !== undefined ? String(payload.toolCall) : undefined;
            const phase = payload.phase !== undefined ? String(payload.phase) : undefined;
            const updatedAt = toEpochSeconds(payload.timestamp);
            rememberPreviewRunId(payload.runId);
            addConsoleLog(`[openclaw_node_progress] node=${neId} phase=${phase || '-'} progress=${progressPct ?? '-'}%`);
            // Update canvas node data for AgentNode rendering
            const canvasNodeId = resolveCanvasNodeId(payload);
            if (canvasNodeId) {
                const existingNode = nodes.find((node) => node.id === canvasNodeId);
                const existingData = (existingNode?.data || {}) as Record<string, unknown>;
                const update: Record<string, unknown> = {};
                const incomingSkills = Array.isArray(payload.loadedSkills) ? payload.loadedSkills.map(String) : [];
                const existingSkills = Array.isArray(existingData.loadedSkills) ? (existingData.loadedSkills as string[]) : [];
                const mergedSkills = [...new Set([...existingSkills, ...incomingSkills])];
                if (payload.nodeKey !== undefined) {
                    update.rawNodeKey = String(payload.nodeKey || '');
                    update.nodeType = normalizeCanvasNodeType(payload.nodeKey || existingData.nodeType || 'builder');
                }
                update.runtime = String(payload.runtime || 'openclaw');
                if (progressPct !== undefined) update.progress = Math.max(0, Math.min(100, progressPct));
                if (partialOutput) {
                    update.outputSummary = buildReadableCurrentWork({
                        lang,
                        nodeType: String(existingData.nodeType || payload.nodeKey || 'builder'),
                        status: String(existingData.status || 'running'),
                        phase: phase || String(existingData.phase || ''),
                        taskDescription: String(existingData.taskDescription || ''),
                        loadedSkills: mergedSkills,
                        outputSummary: '',
                        lastOutput: partialOutput,
                        logs: Array.isArray(payload.activityLog)
                            ? (payload.activityLog as Array<{ ts?: number; msg?: string; type?: string }>)
                            : (Array.isArray(existingData.log) ? (existingData.log as Array<{ ts?: number; msg?: string; type?: string }>) : []),
                    });
                    update.lastOutput = partialOutput;
                }
                if (mergedSkills.length > 0) update.loadedSkills = mergedSkills;
                if (phase) update.phase = phase;
                if (toolCall) update.toolCall = toolCall;
                if (payload.modelLatencyMs !== undefined && Number(payload.modelLatencyMs) > 0) {
                    update.modelLatencyMs = Number(payload.modelLatencyMs);
                }
                if (payload.codeLines !== undefined && Number(payload.codeLines) > 0) {
                    update.codeLines = Number(payload.codeLines);
                }
                if (payload.codeLanguages !== undefined && Array.isArray(payload.codeLanguages) && payload.codeLanguages.length > 0) {
                    update.codeLanguages = (payload.codeLanguages as string[]).map(String);
                }
                if (Object.keys(update).length) updateNodeData(canvasNodeId, update);
                if (Array.isArray(payload.activityLog) && payload.activityLog.length > 0) {
                    const normalizedActivityLog = (payload.activityLog as Array<{ ts?: number; msg?: string; type?: string }>)
                        .filter((entry) => typeof entry?.msg === 'string' && String(entry.msg).trim().length > 0)
                        .map((entry) => ({
                            ts: entry.ts,
                            msg: String(entry.msg).trim(),
                            type: entry.type,
                        }));
                    mergeCanvasNodeLogs(
                        canvasNodeId,
                        normalizedActivityLog,
                    );
                }
                if (phase) {
                    appendCanvasNodeLog(canvasNodeId, {
                        ts: updatedAt * 1000,
                        msg: lang === 'zh' ? `阶段：${phase}` : `Phase: ${phase}`,
                        type: 'info',
                    });
                }
                if (toolCall) {
                    appendCanvasNodeLog(canvasNodeId, {
                        ts: updatedAt * 1000,
                        msg: lang === 'zh' ? `工具调用：${toolCall}` : `Tool call: ${toolCall}`,
                        type: 'sys',
                    });
                }
                if (partialOutput) {
                    const partialDescriptor = describeNodeActivity(partialOutput, lang, {
                        nodeType: String(existingData.nodeType || payload.nodeKey || 'builder'),
                        status: String(existingData.status || 'running'),
                    });
                    if (partialDescriptor && !partialDescriptor.lowSignal) {
                        appendCanvasNodeLog(canvasNodeId, {
                            ts: updatedAt * 1000,
                            msg: partialDescriptor.text,
                            type: partialDescriptor.type,
                        });
                    }
                }
            }
            // Merge into canonical NE state
            if (neId && onMergeNodeExecution) {
                const progressPatch: Partial<NodeExecutionRecord> & Pick<NodeExecutionRecord, 'id' | 'run_id'> = {
                    id: neId,
                    run_id: runId,
                    updated_at: updatedAt,
                };
                if (partialOutput) progressPatch.output_summary = partialOutput;
                if (progressPct !== undefined) progressPatch.progress = progressPct;
                if (phase) progressPatch.phase = phase;
                if (payload.currentAction !== undefined) progressPatch.current_action = String(payload.currentAction || '');
                if (Array.isArray(payload.workSummary)) progressPatch.work_summary = payload.workSummary.map(String);
                if (payload.toolCallStats && typeof payload.toolCallStats === 'object') {
                    progressPatch.tool_call_stats = Object.fromEntries(
                        Object.entries(payload.toolCallStats as Record<string, unknown>).map(([key, value]) => [key, Number(value || 0)]),
                    );
                }
                if (payload.blockingReason !== undefined) progressPatch.blocking_reason = String(payload.blockingReason || '');
                if (typeof payload._neVersion === 'number') progressPatch.version = payload._neVersion;
                onMergeNodeExecution(progressPatch);
            }

        } else if (t === 'openclaw_attach_artifact') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const artifact = payload.artifact as Record<string, unknown> | undefined;
            rememberPreviewRunId(payload.runId);
            addConsoleLog(`[openclaw_attach_artifact] artifact=${String(artifact?.id || 'unknown')} type=${String(artifact?.type || 'unknown')}`);

        } else if (t === 'openclaw_submit_review') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const decision = String(payload.decision || '').trim().toLowerCase();
            const updatedAt = toEpochSeconds(payload.timestamp);
            rememberPreviewRunId(payload.runId);
            addConsoleLog(`[openclaw_submit_review] decision=${String(payload.decision || '')} issues=${JSON.stringify(payload.issues || [])}`);
            // P1-3: Visible review event
            const reviewText = connectorReviewLabel(decision, lang);
            const reviewLabel = lang === 'zh' ? `审核结论: ${reviewText}` : `Review: ${reviewText}`;
            const reviewColor = decision === 'approve' ? 'var(--green)' : (decision === 'reject' || decision === 'blocked' ? 'var(--red)' : 'var(--orange)');
            addMessage('system', reviewLabel, 'OpenClaw', 'OC', reviewColor);
            emitConnectorEvent(
                'openclaw_submit_review',
                reviewLabel,
                Array.isArray(payload.issues) ? payload.issues.slice(0, 2).map(String).join(' · ') : undefined,
                updatedAt * 1000,
            );
            // ── P0-1: Merge review verdict into canonical task state ──
            if (onMergeTask && payload.taskId) {
                const issues = Array.isArray(payload.issues) ? payload.issues.map(String) : [];
                const risks = Array.isArray(payload.remainingRisks) ? payload.remainingRisks.map(String) : [];
                const taskPatch: Partial<TaskCard> & Pick<TaskCard, 'id'> = {
                    id: String(payload.taskId),
                    reviewVerdict: decision,
                    reviewIssues: issues,
                    latestRisk: risks[0] || '',
                    updatedAt: updatedAt * 1000,
                    // P0-3: pass version from broadcast
                    ...(typeof payload._taskVersion === 'number' ? { version: payload._taskVersion } : {}),
                };
                if (['needs_fix', 'reject', 'blocked'].includes(decision)) {
                    taskPatch.status = 'review';
                }
                onMergeTask(taskPatch);
            }
            // Mirror backend semantics: only negative review states block the run.
            if (onMergeRun && payload.runId && ['needs_fix', 'reject', 'blocked'].includes(decision)) {
                onMergeRun(clearRunActivity({
                    id: String(payload.runId),
                    status: 'waiting_review' as RunRecord['status'],
                    updated_at: updatedAt,
                    // P0-3: pass version from broadcast
                    ...(typeof payload._runVersion === 'number' ? { version: payload._runVersion } : {}),
                }));
            }

        } else if (t === 'openclaw_submit_validation') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const summaryStatus = String(payload.summaryStatus || '').trim().toLowerCase();
            const updatedAt = toEpochSeconds(payload.timestamp);
            rememberPreviewRunId(payload.runId);
            addConsoleLog(`[openclaw_submit_validation] summaryStatus=${summaryStatus} summary=${String(payload.summary || '')}`);
            // P1-3: Visible selfcheck event
            const validationText = connectorValidationLabel(summaryStatus, lang);
            const checkLabel = lang === 'zh' ? `自检结果: ${validationText}` : `Selfcheck: ${validationText}`;
            const validationColor = summaryStatus === 'passed' ? 'var(--green)' : (summaryStatus === 'failed' ? 'var(--red)' : 'var(--orange)');
            addMessage('system', checkLabel, 'OpenClaw', 'OC', validationColor);
            emitConnectorEvent(
                'openclaw_submit_validation',
                checkLabel,
                String(payload.summary || '').slice(0, 120) || undefined,
                updatedAt * 1000,
            );
            if (onMergeTask && payload.taskId) {
                const checklist = Array.isArray(payload.checklist)
                    ? payload.checklist.map((item: Record<string, unknown>) => ({
                        name: String(item.name || ''),
                        passed: String(item.status || '').toLowerCase() === 'passed',
                        detail: String(item.detail || ''),
                    }))
                    : [];
                const taskPatch: Partial<TaskCard> & Pick<TaskCard, 'id'> = {
                    id: String(payload.taskId),
                    selfcheckItems: checklist,
                    latestSummary: String(payload.summary || ''),
                    updatedAt: updatedAt * 1000,
                    // P0-3: pass version from broadcast
                    ...(typeof payload._taskVersion === 'number' ? { version: payload._taskVersion } : {}),
                };
                if (['failed', 'blocked'].includes(summaryStatus)) {
                    taskPatch.status = 'selfcheck';
                }
                onMergeTask(taskPatch);
            }
            if (onMergeRun && payload.runId && ['failed', 'blocked'].includes(summaryStatus)) {
                onMergeRun(clearRunActivity({
                    id: String(payload.runId),
                    status: 'waiting_selfcheck' as RunRecord['status'],
                    updated_at: updatedAt,
                    // P0-3: pass version from broadcast
                    ...(typeof payload._runVersion === 'number' ? { version: payload._runVersion } : {}),
                }));
            }

        } else if (t === 'openclaw_run_complete') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const finalResult = String(payload.finalResult || '').trim().toLowerCase();
            const success = payload.success === true || finalResult === 'success' || finalResult === 'done';
            const updatedAt = toEpochSeconds(payload.timestamp);
            const rawCompletionPreviewUrl = String(payload.previewUrl || payload.preview_url || '').trim();
            let completionPreviewUrl = '';
            if (rawCompletionPreviewUrl) {
                try { completionPreviewUrl = new URL(rawCompletionPreviewUrl, 'http://127.0.0.1:8765').toString(); } catch { completionPreviewUrl = rawCompletionPreviewUrl; }
                runPreviewUrlRef.current = completionPreviewUrl;
                setPreviewUrl(withCacheBust(completionPreviewUrl));
            }
            rememberPreviewRunId(payload.runId);
            addConsoleLog(`[openclaw_run_complete] runId=${String(payload.runId || '')} result=${finalResult}`);
            emitConnectorEvent(
                'openclaw_run_complete',
                success
                    ? tr('运行已完成', 'Run completed')
                    : tr('运行失败', 'Run failed'),
                String(payload.summary || payload.finalResult || '').slice(0, 120) || undefined,
                updatedAt * 1000,
            );
            // ── P0-1: Merge terminal run status into canonical state ──
            if (onMergeRun && payload.runId) {
                onMergeRun(clearRunActivity({
                    id: String(payload.runId),
                    status: (success ? 'done' : 'failed') as RunRecord['status'],
                    summary: String(payload.summary || payload.finalResult || ''),
                    risks: Array.isArray(payload.risks) ? payload.risks.map(String) : [],
                    total_tokens: Number(payload.totalTokens || 0),
                    total_cost: Number(payload.totalCost || 0),
                    ended_at: updatedAt,
                    updated_at: updatedAt,
                    // P0-3: pass version from broadcast
                    ...(typeof payload._runVersion === 'number' ? { version: payload._runVersion } : {}),
                }));
            }
            // Update task status to done/failed
            if (onMergeTask && payload.taskId) {
                onMergeTask({
                    id: String(payload.taskId),
                    status: success ? 'done' : 'executing',
                    latestSummary: String(payload.summary || payload.finalResult || ''),
                    latestRisk: Array.isArray(payload.risks) ? String(payload.risks[0] || '') : '',
                    updatedAt: updatedAt * 1000,
                    // P0-3: pass version from broadcast
                    ...(typeof payload._taskVersion === 'number' ? { version: payload._taskVersion } : {}),
                });
            }

            // v3.1: Always clear running state on run_complete, even if no subtask details.
            setRunning(false);
            // §FIX: Display completion report card (same as orchestrator_complete)
            const ocSubtasks = Array.isArray(payload.subtasks) ? payload.subtasks as Array<{
                id: string; agent: string; status: string; retries: number;
                work_summary?: string[]; files_created?: string[]; error?: string;
            }> : [];
            if (ocSubtasks.length > 0) {
                const diffRaw = String(payload.difficulty || difficulty).toLowerCase();
                const runDifficulty: 'simple' | 'standard' | 'pro' = (
                    diffRaw === 'simple' || diffRaw === 'pro' || diffRaw === 'standard'
                ) ? diffRaw : 'standard';
                const completionData: import('@/lib/types').ChatMessage['completionData'] = {
                    success,
                    completed: Number(payload.completed || 0),
                    total: Number(payload.total_subtasks || 0),
                    retries: Number(payload.total_retries || 0),
                    durationSeconds: Number(payload.duration_seconds || 0),
                    difficulty: runDifficulty,
                    subtasks: ocSubtasks.map(st => ({
                        id: String(st.id),
                        agent: String(st.agent || 'agent'),
                        status: String(st.status || 'unknown'),
                        retries: Number(st.retries || 0),
                        filesCreated: Array.isArray(st.files_created) ? st.files_created : undefined,
                        workSummary: Array.isArray(st.work_summary) ? st.work_summary : undefined,
                        codeLines: Number((st as any).code_lines || 0) || undefined,
                        codeKb: Number((st as any).code_kb || 0) || undefined,
                        codeLanguages: Array.isArray((st as any).code_languages) ? (st as any).code_languages : undefined,
                    })),
                    previewUrl: completionPreviewUrl || runPreviewUrlRef.current || undefined,
                };
                const dur = Number(payload.duration_seconds || 0);
                const durText = dur >= 60
                    ? `${Math.floor(dur / 60)} 分 ${Math.round(dur % 60)} 秒`
                    : `${Math.round(dur)}s`;
                addMessage('system',
                    tr(
                        `<b>执行完成</b>：${payload.completed}/${payload.total_subtasks} 节点，耗时 ${durText}`,
                        `<b>Run completed</b>: ${payload.completed}/${payload.total_subtasks} nodes, ${durText}`),
                    'Report', '🏁', success ? 'var(--green)' : 'var(--orange)', completionData);
            }

            // §FIX: Auto-open preview on OpenClaw completion
            if (success && (completionPreviewUrl || runPreviewUrlRef.current)) {
                setCanvasView('preview');
                previewReadyForRunRef.current = true;
            }

        // ── G2: REST endpoint broadcast handlers (multi-client sync) ──
        } else if (t === 'task_created' || t === 'task_updated' || t === 'task_transitioned') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const taskData = (payload.task || payload) as Record<string, unknown>;
            if (onMergeTask && taskData.id) {
                onMergeTask({ id: String(taskData.id), ...taskData } as Partial<TaskCard> & Pick<TaskCard, 'id'>);
            }
            addConsoleLog(`[${t}] taskId=${String(taskData.id || '')}`);

        } else if (t === 'run_created' || t === 'run_updated' || t === 'run_transitioned') {
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const runData = (payload.run || payload) as Record<string, unknown>;
            if (onMergeRun && runData.id) {
                onMergeRun({ id: String(runData.id), ...runData } as Partial<RunRecord> & Pick<RunRecord, 'id'>);
            }
            addConsoleLog(`[${t}] runId=${String(runData.id || '')}`);
            if (t === 'run_created') {
                emitConnectorEvent(
                    'run_created',
                    tr('运行已创建', 'Run created'),
                    `${String(runData.id || '')} · ${String(runData.runtime || 'local')}`,
                );
            }
        }
    }, [addMessage, appendCanvasNodeLog, appendSubtaskTimeline, buildPlanNodes, clearPreviewFallbackTimer, difficulty, emitConnectorEvent, lang, mergeCanvasNodeLogs, messages, nodes, onMergeNodeExecution, onMergeRun, onMergeTask, rememberPreviewRunId, resetRunState, resolveCanvasNodeId, updateNodeData, addReport]);

    const { connected, sendGoal, runWorkflow: wsRunWorkflow, stop, reconnect, wsRef, send } = useWebSocket({ url: wsUrl, onMessage: onWSMessage });

    useEffect(() => {
        if (connected) return;
        setConnectorConnectedAt(null);
    }, [connected]);

    // ── Send goal ──
    const handleSendGoal = useCallback((goal: string, attachments: ChatAttachment[] = []) => {
        const normalizedGoal = goal.trim();
        const effectiveGoal = normalizedGoal || defaultGoalFromAttachments(lang);
        addMessage('user', effectiveGoal, 'You', '👤', undefined, undefined, attachments);
        if (connected) {
            resetRunState();
            const recentHistory = messages
                .filter((m) => isHistoryConversationMessage(m) && (m.content.trim() || m.completionData))
                .slice(-7)
                .map((m) => {
                    const completionSummary = m.completionData
                        ? `Run summary: ${m.completionData.completed}/${m.completionData.total} nodes completed, retries=${m.completionData.retries}, duration=${m.completionData.durationSeconds}s.`
                        : '';
                    const historyContent = [m.content, completionSummary].filter(Boolean).join('\n').trim();
                    return {
                        role: m.role,
                        content: buildMessageContentForHistory(historyContent, m.attachments || []),
                    };
                });
            recentHistory.push({ role: 'user', content: buildMessageContentForHistory(effectiveGoal, attachments) });
            const effectiveGoalRuntime = goalRuntime === 'openclaw' ? 'openclaw' : 'local';
            const userCanvasPlan = buildRunGoalPlan(nodes, edges);
            if (effectiveGoalRuntime === 'openclaw') {
                addMessage(
                    'system',
                    lang === 'zh'
                        ? '☁ OpenClaw Direct Mode — 任务将通过 OpenClaw 直接派发执行'
                        : '☁ OpenClaw Direct Mode — Tasks will be dispatched directly via OpenClaw',
                    'OpenClaw',
                    'OC',
                    '#a855f7',
                );
            }
            if (userCanvasPlan) {
                addMessage(
                    'system',
                    lang === 'zh'
                        ? `检测到你当前画布上的 ${userCanvasPlan.nodes.length} 个自定义节点，本轮会优先按你的节点编排执行。`
                        : `Detected ${userCanvasPlan.nodes.length} custom canvas nodes. This run will follow your canvas workflow first.`,
                    'Evermind',
                    '🧭',
                );
            }
            sendGoal(
                effectiveGoal,
                undefined,
                recentHistory,
                difficulty,
                effectiveGoalRuntime,
                sessionId,
                attachments,
                userCanvasPlan,
            );
            addMessage('system', lang === 'zh' ? '已收到目标，正在规划执行...' : 'Goal received — planning...', 'Evermind', '🧠');
        } else {
            addMessage('system',
                lang === 'zh' ? '后端未连接。请运行：<code>cd backend && python server.py</code>' : 'Backend offline. Run: <code>cd backend && python server.py</code>',
                'System', '⚠️');
        }
    }, [connected, sendGoal, addMessage, messages, difficulty, goalRuntime, lang, resetRunState, sessionId, nodes, edges]);

    // ── Run workflow from canvas ──
    const handleRun = useCallback(() => {
        if (!connected) return;
        resetRunState();
        setPreviewUrl(null);
        setCanvasView('editor');
        const lastUserMsg = [...messages].reverse().find(m => m.role === 'user');
        if (lastUserMsg) {
            const updatedNodes = nodes.map((n, i) => {
                const nt = n.data?.nodeType as string;
                if (nt === 'router' && i === nodes.findIndex(nd => (nd.data?.nodeType as string) === 'router')) {
                    return { ...n, data: { ...n.data, _direct_input: lastUserMsg.content } };
                }
                return n;
            });
            setNodes(updatedNodes);
            wsRunWorkflow(updatedNodes, edges);
        } else {
            wsRunWorkflow(nodes, edges);
        }
    }, [connected, resetRunState, messages, nodes, edges, setNodes, wsRunWorkflow]);

    const handleStop = useCallback(() => {
        clearPreviewFallbackTimer();
        stop();
        setRunning(false);
    }, [clearPreviewFallbackTimer, stop]);

    // ── OpenClaw V1 Connector: Evermind → OpenClaw ──
    const dispatchNode = useCallback((payload: Record<string, unknown>) => {
        const idempotencyKey = `evermind_dispatch_node:${payload.runId}:${payload.nodeExecutionId}:${payload.retryCount || 0}`;
        send({
            type: 'evermind_dispatch_node',
            requestId: `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
            idempotencyKey,
            timestamp: Date.now(),
            payload,
        });
    }, [send]);

    const cancelRunWS = useCallback((runId: string) => {
        const idempotencyKey = `evermind_cancel_run:${runId}`;
        send({
            type: 'evermind_cancel_run',
            requestId: `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
            idempotencyKey,
            timestamp: Date.now(),
            payload: { runId },
        });
    }, [send]);

    const resumeRunWS = useCallback((runId: string) => {
        const idempotencyKey = `evermind_resume_run:${runId}`;
        send({
            type: 'evermind_resume_run',
            requestId: `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
            idempotencyKey,
            timestamp: Date.now(),
            payload: { runId },
        });
    }, [send]);

    const rerunNodeWS = useCallback((neId: string) => {
        const idempotencyKey = `evermind_rerun_node:${neId}`;
        send({
            type: 'evermind_rerun_node',
            requestId: `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
            idempotencyKey,
            timestamp: Date.now(),
            payload: { nodeExecutionId: neId },
        });
    }, [send]);

    // P1-2C: Reconnect re-dispatch — fetch stale nodes and auto-dispatch
    const recheckStaleNodes = useCallback(async (runId: string) => {
        if (!runId || !connected) return;
        try {
            const apiBase = wsUrl.replace('ws://', 'http://').replace('wss://', 'https://').replace('/ws', '');
            const resp = await fetch(`${apiBase}/api/runs/${runId}/stale-nodes?stale_threshold_s=60`);
            if (!resp.ok) return;
            const data = await resp.json() as { staleNodes: Array<{ id: string; node_key: string }>; runtime: string };
            if (data.runtime !== 'openclaw') return;
            for (const staleNode of data.staleNodes) {
                console.log(`[Reconnect] Re-dispatching stale node: ${staleNode.id} (key=${staleNode.node_key})`);
                dispatchNode({
                    runId,
                    nodeExecutionId: staleNode.id,
                    nodeKey: staleNode.node_key,
                    reconnectRedispatch: true,
                });
            }
        } catch (err) {
            console.warn('[Reconnect] Failed to check stale nodes:', err);
        }
    }, [connected, wsUrl, dispatchNode]);

    useEffect(() => {
        const wasConnected = previousConnectedRef.current;
        previousConnectedRef.current = connected;
        if (!connected || wasConnected) return;

        const uniqueRunIds = [...new Set(reconnectRunIds.filter(Boolean))];
        for (const runId of uniqueRunIds) {
            void recheckStaleNodes(runId);
        }
    }, [connected, reconnectRunIds, recheckStaleNodes]);

    // ── Deep Link Handler: clean state injection from evermind:// ──
    // Uses refs for stable access to latest values across re-renders
    const pendingDeepLinkGoalRef = useRef<string | null>(null);
    const connectedRef = useRef(connected);
    const handleSendGoalRef = useRef(handleSendGoal);
    connectedRef.current = connected;
    handleSendGoalRef.current = handleSendGoal;

    // Listen for the deep link event dispatched by Electron
    useEffect(() => {
        const handleDeepLink = (e: Event) => {
            const goal = (e as CustomEvent)?.detail?.goal;
            if (!goal || typeof goal !== 'string' || !goal.trim()) return;

            console.log('[DeepLink] Frontend received goal:', goal);

            // 1. Stop any running execution
            setRunning(false);

            // 2. Reset all run-scoped state
            resetRunState();

            // 3. Clear canvas nodes (remove stale workflow graph)
            setNodes([]);

            // 4. Clear preview
            setPreviewUrl(null);
            setCanvasView('editor');

            // 5. Clear the global marker so Electron knows we consumed it
            if (typeof window !== 'undefined') {
                (window as unknown as Record<string, unknown>).__EVERMIND_DEEPLINK_GOAL = undefined;
            }

            // 6. Store goal and attempt submission
            const trimmedGoal = goal.trim();
            pendingDeepLinkGoalRef.current = trimmedGoal;

            if (connectedRef.current) {
                // WS already connected — submit immediately after a micro-delay
                console.log('[DeepLink] WS connected, submitting goal now');
                setTimeout(() => {
                    const g = pendingDeepLinkGoalRef.current;
                    if (g) {
                        pendingDeepLinkGoalRef.current = null;
                        handleSendGoalRef.current(g);
                        console.log('[DeepLink] Goal submitted to backend:', g.substring(0, 60));
                    }
                }, 200);
            } else {
                console.log('[DeepLink] WS not connected yet, goal queued for connection watcher');
                // The connection watcher effect below will handle submission
            }
        };

        window.addEventListener('evermind-deeplink', handleDeepLink);
        return () => window.removeEventListener('evermind-deeplink', handleDeepLink);
    }, [resetRunState, setNodes, setPreviewUrl, setCanvasView]);

    // Connection watcher: when WS connects and there's a pending deep link goal, submit it
    useEffect(() => {
        if (connected && pendingDeepLinkGoalRef.current) {
            const goal = pendingDeepLinkGoalRef.current;
            pendingDeepLinkGoalRef.current = null;
            console.log('[DeepLink] WS just connected, submitting queued goal:', goal);
            setTimeout(() => {
                handleSendGoal(goal);
                console.log('[DeepLink] Queued goal submitted to backend:', goal.substring(0, 60));
            }, 300);
        }
    }, [connected, handleSendGoal]);

    return {
        running, previewUrl, previewRunId, canvasView, setCanvasView, connected, wsRef,
        handleSendGoal, handleRun, handleStop, setPreviewUrl,
        dispatchNode, cancelRunWS, resumeRunWS, rerunNodeWS, recheckStaleNodes,
        reconnect,
        connectorRuntimeId,
        connectorPid,
        connectorConnectedAt,
        connectorLastEventAt,
    };
}
