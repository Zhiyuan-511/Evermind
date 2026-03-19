'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useWebSocket } from '@/hooks/useWebSocket';
import type { ChatMessage, RunReportRecord, TaskCard, RunRecord, NodeExecutionRecord } from '@/lib/types';
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

function clearRunActivity(runPatch: Partial<RunRecord> & Pick<RunRecord, 'id'>): Partial<RunRecord> & Pick<RunRecord, 'id'> {
    return {
        ...runPatch,
        current_node_execution_id: '',
        active_node_execution_ids: [],
    };
}

// Preview validation
interface ClientPreviewValidation {
    ok: boolean; status: number; bytes: number; errors: string[]; warnings: string[];
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

// ── Hook Options ──
export interface UseRuntimeConnectionOptions {
    wsUrl: string;
    lang: 'en' | 'zh';
    difficulty: 'simple' | 'standard' | 'pro';
    messages: ChatMessage[];
    addMessage: (role: 'user' | 'system' | 'agent', content: string, sender?: string, icon?: string, borderColor?: string) => void;
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
}

export interface UseRuntimeConnectionReturn {
    running: boolean;
    previewUrl: string | null;
    previewRunId: string | null;
    canvasView: 'editor' | 'preview';
    setCanvasView: React.Dispatch<React.SetStateAction<'editor' | 'preview'>>;
    connected: boolean;
    wsRef: React.RefObject<WebSocket | null>;
    handleSendGoal: (goal: string) => void;
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
}

export function useRuntimeConnection({
    wsUrl, lang, difficulty, messages, addMessage, addReport,
    buildPlanNodes, updateNodeData, nodes, edges, setNodes,
    onMergeTask, onMergeRun, onMergeNodeExecution, reconnectRunIds = [],
}: UseRuntimeConnectionOptions): UseRuntimeConnectionReturn {
    const [running, setRunning] = useState(false);
    const [previewUrl, setPreviewUrl] = useState<string | null>(null);
    const [previewRunId, setPreviewRunId] = useState<string | null>(null);
    const [canvasView, setCanvasView] = useState<'editor' | 'preview'>('editor');

    // Run-scoped refs
    const subtaskNodeMap = useRef<Record<string, string>>({});
    const runStartedAtRef = useRef<number>(0);
    const previewReadyForRunRef = useRef<boolean>(false);
    const previewFallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const runPreviewUrlRef = useRef<string>('');
    const runSubtasksRef = useRef<Record<string, {
        task?: string; agent?: string; output?: string; error?: string;
        status?: string; retries?: number; startedAt?: number; endedAt?: number;
        durationSeconds?: number; timelineEvents?: string[];
    }>>({});
    const browserModeNotifiedRef = useRef<Record<string, boolean>>({});
    const waitingAiLastNotifyRef = useRef<Record<string, number>>({});
    const previousConnectedRef = useRef(false);

    const appendSubtaskTimeline = useCallback((subtaskId: string, line: string) => {
        if (!subtaskId || !line.trim()) return;
        const prev = runSubtasksRef.current[subtaskId] || {};
        const events = [...(prev.timelineEvents || []), line.trim()].slice(-30);
        runSubtasksRef.current[subtaskId] = { ...prev, timelineEvents: events };
    }, []);

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
        const subtaskId = String(payload.subtaskId || '').trim();
        const nodeLabel = String(payload.nodeLabel || '').trim();

        const exactMatch = nodes.find((node) =>
            (nodeExecutionId && String(node.data?.nodeExecutionId || '').trim() === nodeExecutionId) ||
            (nodeKey && node.id === nodeKey) ||
            (subtaskId && String(node.data?.subtaskId || '').trim() === subtaskId),
        );
        if (exactMatch) return exactMatch.id;

        if (nodeKey) {
            const typeMatches = nodes.filter((node) => String(node.data?.nodeType || '').trim() === nodeKey);
            if (typeMatches.length === 1) return typeMatches[0].id;
            const activeTypeMatch = typeMatches.find((node) => ['running', 'queued', 'blocked', 'waiting_approval'].includes(String(node.data?.status || '').trim()));
            if (activeTypeMatch) return activeTypeMatch.id;
            const unboundTypeMatch = typeMatches.find((node) => !String(node.data?.nodeExecutionId || '').trim());
            if (unboundTypeMatch) return unboundTypeMatch.id;
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
        setPreviewRunId(null);
        runSubtasksRef.current = {};
        browserModeNotifiedRef.current = {};
        waitingAiLastNotifyRef.current = {};
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
            addMessage('system',
                tr(`🟢 后端已连接：v${version} · runtime ${runtimeId} · pid ${pid}`, `🟢 Backend connected: v${version} · runtime ${runtimeId} · pid ${pid}`),
                'System', '🟢', 'var(--green)');
            addConsoleLog(`[connected] version=${version} runtime=${runtimeId} pid=${pid}`);

        } else if (t === 'orchestrator_start') {
            setRunning(true);
            resetRunState();
            setPreviewUrl(null);
            setCanvasView('editor');
            addMessage('system', tr('🧠 已接收目标，正在规划执行...', '🧠 Goal received. Planning execution...'), 'Orchestrator', '🧠');
            addConsoleLog(`[orchestrator_start] difficulty=${String(msg.difficulty || 'standard')}`);

        } else if (t === 'plan_created') {
            const subtasks = (msg.subtasks as Array<{ id: string; agent: string; task: string; depends_on: string[] }>) || [];
            addMessage('system',
                tr(`📋 计划已创建：${msg.total} 个节点（详细执行步骤已写入日志）`, `📋 Plan created: ${msg.total} nodes (detailed execution is in logs)`),
                'Plan', '📋');
            try { console.info('[Evermind][Plan]', subtasks); } catch { /* noop */ }
            addConsoleLog(`[plan_created] total=${String(msg.total || subtasks.length)}`);
            subtasks.forEach(st => {
                addConsoleLog(`[plan] #${st.id} ${st.agent} deps=[${(st.depends_on || []).join(',')}] task=${(st.task || '').slice(0, 120)}`);
            });
            subtaskNodeMap.current = buildPlanNodes(subtasks, lang);

        } else if (t === 'subtask_start') {
            const subtaskId = msg.subtask_id as string;
            const agentName = String(msg.agent || 'agent');
            const startedAt = Date.now();
            runSubtasksRef.current[subtaskId] = {
                ...(runSubtasksRef.current[subtaskId] || {}),
                agent: agentName, task: String(msg.task || ''),
                status: 'running', startedAt, endedAt: undefined, durationSeconds: undefined,
            };
            appendSubtaskTimeline(subtaskId, `开始执行：${agentName} 接收任务并进入处理流程。`);
            addMessage('system', tr(`⚙️ ${agentName} #${subtaskId} 开始执行`, `⚙️ ${agentName} #${subtaskId} started`), `${agentName} #${subtaskId}`, '⚙️', 'var(--blue)');
            addConsoleLog(`[subtask_start] #${subtaskId} agent=${agentName} task=${String(msg.task || '').slice(0, 180)}`);
            const canvasNodeId = subtaskNodeMap.current[subtaskId];
            if (canvasNodeId) updateNodeData(canvasNodeId, { status: 'running', progress: 30, startedAt });

        } else if (t === 'subtask_complete') {
            const subtaskId = msg.subtask_id as string;
            const success = msg.success as boolean;
            const agentName = (msg.agent as string) || 'Agent';
            const fullOutput = ((msg.full_output || msg.output_preview || '') as string);
            const err = String(msg.error || '');
            const prevState = runSubtasksRef.current[subtaskId] || {};
            const endedAt = Date.now();
            const startedAt = prevState.startedAt || endedAt;
            const durationSeconds = Math.max(0, Math.round((endedAt - startedAt) / 1000));
            runSubtasksRef.current[subtaskId] = {
                ...prevState, agent: agentName,
                status: success ? 'completed' : 'failed',
                output: fullOutput, error: err, endedAt, durationSeconds,
            };
            appendSubtaskTimeline(subtaskId, success
                ? `执行完成：${agentName} 已完成，耗时约 ${durationSeconds} 秒。`
                : `执行失败：${agentName} 结束于失败，耗时约 ${durationSeconds} 秒。`);
            if (err) appendSubtaskTimeline(subtaskId, `失败原因：${err.slice(0, 160)}`);
            addMessage('system',
                success
                    ? tr(`✅ ${agentName} #${subtaskId} 已完成`, `✅ ${agentName} #${subtaskId} completed`)
                    : tr(`❌ ${agentName} #${subtaskId} 执行失败（细节见日志）`, `❌ ${agentName} #${subtaskId} failed (see logs)`),
                `${agentName} #${subtaskId}`, success ? '✅' : '❌', success ? 'var(--green)' : 'var(--red)');
            try { console.info(`[Evermind][Subtask ${subtaskId}]`, { agent: agentName, success, output_len: fullOutput.length, output_preview: fullOutput.slice(0, 1200) }); } catch { /* noop */ }
            addConsoleLog(`[subtask_complete] #${subtaskId} agent=${agentName} success=${String(success)} output_len=${fullOutput.length}${err ? ` error=${err.slice(0, 160)}` : ''}`);
            const canvasNodeId = subtaskNodeMap.current[subtaskId];
            if (canvasNodeId) updateNodeData(canvasNodeId, {
                status: success ? 'passed' : 'failed',
                progress: 100,
                lastOutput: fullOutput.substring(0, 2000),
                endedAt,
                startedAt: prevState.startedAt || endedAt,
            });

        } else if (t === 'files_created') {
            const files = (msg.files as string[]) || [];
            const outputDir = (msg.output_dir as string) || '/tmp/evermind_output';
            if (files.length > 0) {
                addMessage('system',
                    tr(`📁 产物已更新：${files.length} 个文件（目录：<code>${outputDir}</code>）`, `📁 Artifacts updated: ${files.length} files (dir: <code>${outputDir}</code>)`),
                    'File Output', '📁', 'var(--green)');
                addConsoleLog(`[files_created] count=${files.length} dir=${outputDir}`);
            }

        } else if (t === 'preview_ready') {
            const rawPreviewUrl = msg.preview_url as string;
            const files = (msg.files as string[]) || [];
            if (rawPreviewUrl) {
                previewReadyForRunRef.current = true;
                clearPreviewFallbackTimer();
                let resolvedUrl = rawPreviewUrl.trim();
                try { resolvedUrl = new URL(resolvedUrl, 'http://127.0.0.1:8765').toString(); } catch { /* Keep original */ }
                runPreviewUrlRef.current = resolvedUrl;
                const safePreviewUrl = escapeHtml(resolvedUrl);
                const previewWithBust = withCacheBust(resolvedUrl);
                setPreviewUrl(previewWithBust);
                setCanvasView('preview');
                const shortFiles = files.slice(0, 3).map((f: string) => f.split('/').pop()).join(', ');
                addMessage('system',
                    tr(
                        `🔗 <b>预览已就绪</b>，已自动切换到预览视图。<br/><a href="${safePreviewUrl}" target="_blank" rel="noopener noreferrer">👉 新窗口打开</a>${shortFiles ? `<br/>📄 文件: ${shortFiles}` : ''}`,
                        `🔗 <b>Preview ready</b>, switched to preview view.<br/><a href="${safePreviewUrl}" target="_blank" rel="noopener noreferrer">👉 Open in new window</a>${shortFiles ? `<br/>📄 Files: ${shortFiles}` : ''}`),
                    'Preview', '🔗', 'var(--green)');
                try { console.info('[Evermind][PreviewReady]', { preview_url: resolvedUrl, files, final: Boolean(msg.final) }); } catch { /* noop */ }
                addConsoleLog(`[preview_ready] url=${resolvedUrl} files=${files.length} final=${String(Boolean(msg.final))}`);
                void (async () => {
                    const check = await runClientPreviewValidation(resolvedUrl);
                    if (check.ok) {
                        addMessage('system', tr(`✅ 预览验收通过（HTTP ${check.status}，${check.bytes} bytes）`, `✅ Preview validation passed (HTTP ${check.status}, ${check.bytes} bytes)`), 'Validator', '✅', 'var(--green)');
                    } else {
                        addMessage('system', tr(`❌ 预览验收失败：${check.errors.slice(0, 3).join('；')}`, `❌ Preview validation failed: ${check.errors.slice(0, 3).join('; ')}`), 'Validator', '❌', 'var(--red)');
                    }
                })();
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
            addMessage('system', tr(`🔄 触发重试（第 ${msg.retry}/${msg.max_retries} 次）`, `🔄 Retry triggered (attempt ${msg.retry}/${msg.max_retries})`), `Retry #${msg.subtask_id}`, '🔄', 'var(--yellow)');
            addConsoleLog(`[subtask_retry] #${String(msg.subtask_id)} retry=${String(msg.retry)}/${String(msg.max_retries)} error=${String(msg.error || '').slice(0, 180)}`);
            const canvasNodeId = subtaskNodeMap.current[msg.subtask_id as string];
            if (canvasNodeId) updateNodeData(canvasNodeId, { status: 'running', progress: 10 });

        } else if (t === 'test_failed_retrying') {
            addMessage('system', tr('🔴 测试未通过，正在回滚并重试修复', '🔴 Tests failed, rerunning with repair instructions'), 'Tester', '🧪', 'var(--red)');

        } else if (t === 'orchestrator_complete') {
            setRunning(false);
            const success = msg.success as boolean;
            const subtasks = (msg.subtasks as Array<{
                id: string; agent: string; status: string; retries: number;
                task?: string; output_preview?: string; error?: string;
                work_summary?: string[]; files_created?: string[];
            }>) || [];
            const reportLines = subtasks.map(st =>
                `• #${st.id} ${st.agent}: ${st.status}${st.retries > 0 ? ` (retry ${st.retries})` : ''}`
            ).join('<br/>');
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
                subtasks: subtasks.map((st) => {
                    const runtime = runSubtasksRef.current[String(st.id)] || {};
                    return {
                        id: String(st.id), agent: String(st.agent || runtime.agent || 'agent'),
                        status: String(st.status || runtime.status || 'unknown'),
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
                    const { saveReportApi, createTask, transitionTask: transApi, updateTaskApi } = await import('@/lib/api');
                    const reportPayload: Record<string, unknown> = {
                        ...reportRecord, task_id: reportRecord.taskId || '',
                        subtasks: reportRecord.subtasks.map((st) => ({ ...st, files_created: st.filesCreated || [] })),
                    };
                    await saveReportApi(reportPayload).catch(() => {});
                    const taskTitle = reportRecord.goal?.slice(0, 120) || 'Untitled Run';
                    const { task: newTask } = await createTask({
                        title: taskTitle,
                        description: `Auto-created from run at ${new Date().toLocaleString()}`,
                        mode: runDifficulty === 'pro' ? 'pro' : 'standard', priority: 'medium',
                    });
                    if (newTask?.id) {
                        reportPayload.task_id = newTask.id;
                        await saveReportApi({ ...reportPayload, id: reportRecord.id, task_id: newTask.id }).catch(() => {});
                        await transApi(newTask.id, 'planned').catch(() => {});
                        await transApi(newTask.id, 'executing').catch(() => {});
                        if (success) {
                            await transApi(newTask.id, 'review').catch(() => {});
                            const reviewerSt = subtasks.find(s => s.agent === 'reviewer');
                            if (reviewerSt) {
                                try {
                                    const verdict = JSON.parse(reviewerSt.output_preview || '{}');
                                    await updateTaskApi(newTask.id, {
                                        reviewVerdict: verdict.verdict === 'REJECTED' ? 'rejected' : 'approved',
                                        reviewIssues: verdict.improvements || [],
                                        latestSummary: taskTitle,
                                    } as Partial<RunReportRecord & { reviewVerdict: string; reviewIssues: string[]; latestSummary: string }>).catch(() => {});
                                } catch { /* non-JSON reviewer output */ }
                            }
                        }
                    }
                } catch { /* backend persistence failed */ }
            })();

            addMessage('system',
                tr(
                    `${success ? '✅' : '⚠️'} <b>执行完成</b>：${msg.completed}/${msg.total_subtasks} 节点，重试 ${msg.total_retries} 次，耗时 ${msg.duration_seconds}s<br/><br/><b>节点报告</b><br/>${reportLines}<br/><br/>📑 已生成专业报告：可在左侧 <b>报告</b> 按钮查看历史版本。`,
                    `${success ? '✅' : '⚠️'} <b>Run completed</b>: ${msg.completed}/${msg.total_subtasks} nodes, ${msg.total_retries} retries, ${msg.duration_seconds}s<br/><br/><b>Node report</b><br/>${reportLines}<br/><br/>📑 Professional report generated: open left-side <b>Reports</b> to view history.`),
                'Report', '🏁', success ? 'var(--green)' : 'var(--orange)');
            addConsoleLog(`[orchestrator_complete] success=${String(success)} completed=${String(msg.completed)}/${String(msg.total_subtasks)} retries=${String(msg.total_retries)} duration=${String(msg.duration_seconds)}s`);

            if (success) {
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
                                addMessage('system', tr(`🔗 已自动检测预览：<a href="${url}" target="_blank">打开</a>`, `🔗 Auto-detected preview: <a href="${url}" target="_blank">Open</a>`), 'Preview', '🔗', 'var(--green)');
                                addConsoleLog(`[preview_fallback] accepted latest=${latest} mtime=${String(latestMtime)}`);
                            } else if (latest && !freshEnough) {
                                addMessage('system', tr('⚠️ 检测到的预览文件较旧，已忽略，避免误打开历史产物。', '⚠️ Detected preview artifact is stale, ignored to avoid opening old output.'), 'Preview', '⚠️', 'var(--orange)');
                                addConsoleLog(`[preview_fallback] ignored stale latest=${latest} mtime=${String(latestMtime)} runStarted=${String(runStartedAtRef.current)}`);
                            }
                        } catch { /* ignore */ }
                    })();
                }, 3000);
            }

        } else if (t === 'orchestrator_error') {
            setRunning(false);
            addMessage('system', tr(`❌ 错误：${msg.error}`, `❌ Error: ${msg.error}`), 'Error', '❌', 'var(--red)');
            addConsoleLog(`[orchestrator_error] ${String(msg.error || '').slice(0, 220)}`);

        } else if (t === 'planning_fallback') {
            const fallbackMsg = String(msg.message || 'Planning failed; switched to fallback plan.');
            addMessage('system', tr(`⚠️ ${fallbackMsg}`, `⚠️ ${fallbackMsg}`), 'Planner', '⚠️', 'var(--orange)');
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
                addMessage('system', tr(`🔑 API 密钥已生效（${msg.keys_applied} 个）`, `🔑 API keys applied (${msg.keys_applied})`), 'Config', '⚙️', 'var(--green)');
            }

        } else if (t === 'subtask_progress') {
            const stage = String(msg.stage || '');
            const sid = String(msg.subtask_id || '');
            if (stage === 'error' && msg.message) {
                if (sid) appendSubtaskTimeline(sid, `执行异常：${String(msg.message).slice(0, 180)}`);
                addMessage('system', `⚠️ ${msg.message}`, 'Error', '⚠️', 'var(--orange)');
                addConsoleLog(`[progress] #${sid} stage=error msg=${String(msg.message).slice(0, 220)}`);
            } else if (stage === 'preview_validation') {
                const ok = Boolean(msg.ok);
                const score = msg.score as number | undefined;
                const errors = (msg.errors as string[]) || [];
                if (sid) appendSubtaskTimeline(sid, ok
                    ? `产物验收通过${typeof score === 'number' ? `（score ${score}）` : ''}`
                    : `产物验收失败：${errors.slice(0, 2).join('；') || '规则校验未通过'}`);
                if (ok) addMessage('system', tr(`✅ 产物验收通过${score ? `（score ${score}）` : ''}`, `✅ Artifact validation passed${score ? ` (score ${score})` : ''}`), 'Preview Gate', '✅', 'var(--green)');
                else addMessage('system', tr(`❌ 产物验收失败：${errors.slice(0, 2).join('；')}`, `❌ Artifact validation failed: ${errors.slice(0, 2).join('; ')}`), 'Preview Gate', '❌', 'var(--red)');
                addConsoleLog(`[progress] #${sid} stage=preview_validation ok=${String(ok)} score=${String(score ?? '')}`);
            } else if (stage === 'preview_validation_failed' && msg.message) {
                addMessage('system', `❌ ${msg.message}`, 'Preview Gate', '❌', 'var(--red)');
                addConsoleLog(`[progress] #${sid} stage=preview_validation_failed msg=${String(msg.message).slice(0, 220)}`);
            } else if (stage === 'model_downgrade') {
                const fromModel = String(msg.from_model || '');
                const toModel = String(msg.to_model || '');
                if (sid) appendSubtaskTimeline(sid, `模型降级重试：${fromModel} → ${toModel}`);
                addMessage('system', tr(`🔄 模型降级重试：<code>${fromModel}</code> → <code>${toModel}</code>`, `🔄 Model downgrade retry: <code>${fromModel}</code> → <code>${toModel}</code>`), 'Auto Recovery', '🔄', 'var(--orange)');
                addConsoleLog(`[progress] #${sid} stage=model_downgrade ${fromModel} -> ${toModel}`);
            } else if (stage === 'waiting_ai') {
                const agent = String(msg.agent || 'agent');
                const elapsed = Number(msg.elapsed_sec || 0);
                addConsoleLog(`[progress] #${sid} stage=waiting_ai agent=${agent} elapsed=${elapsed}s`);
                const nowMs = Date.now();
                const last = waitingAiLastNotifyRef.current[sid] || 0;
                if (!last || nowMs - last >= 60000) {
                    waitingAiLastNotifyRef.current[sid] = nowMs;
                    appendSubtaskTimeline(sid, '等待模型响应中……');
                    addMessage('system', tr(`⏳ ${agent} #${sid} 仍在执行中，请稍候...`, `⏳ ${agent} #${sid} is still running, please wait...`), `${agent} #${sid}`, '⏳', 'var(--orange)');
                }
            } else if (stage === 'stream_stalled') {
                const reason = String(msg.reason || '').slice(0, 180);
                addConsoleLog(`[progress] #${sid} stage=stream_stalled reason=${reason}`);
                if (sid) appendSubtaskTimeline(sid, '模型流式输出停滞，已触发快速失败并进入重试。');
                addMessage('system', tr(`⚠️ ${sid ? `#${sid} ` : ''}模型输出停滞，系统将自动快速重试。`, `⚠️ ${sid ? `#${sid} ` : ''}model stream stalled; auto-retrying quickly.`), 'Model', '⚠️', 'var(--orange)');
            } else if (stage === 'builder_loop_guard') {
                const streak = Number(msg.streak || 0);
                const threshold = Number(msg.threshold || 0);
                const reason = String(msg.reason || 'tool_research_loop');
                addConsoleLog(`[progress] #${sid} stage=builder_loop_guard streak=${streak} threshold=${threshold} reason=${reason}`);
                if (sid) appendSubtaskTimeline(sid, `构建者触发循环保护：连续 ${streak} 次工具调用未产出文件，切换为强制文本输出。`);
                addMessage('system', tr(`⚠️ builder #${sid} 触发循环保护，正在强制输出完整 HTML（原因：${reason}）`, `⚠️ builder #${sid} loop guard triggered, forcing full HTML output (reason: ${reason})`), `builder #${sid}`, '⚠️', 'var(--orange)');
            } else if (stage === 'browser_action') {
                const action = String(msg.action || 'unknown');
                const ok = Boolean(msg.ok);
                const mode = String(msg.browser_mode || 'unknown');
                const requestedMode = String(msg.requested_mode || '');
                const url = String(msg.url || '');
                const err = String(msg.error || '');
                const launchNote = String(msg.launch_note || '');
                addConsoleLog(`[browser] #${sid} action=${action} ok=${String(ok)} mode=${mode}${requestedMode ? ` requested=${requestedMode}` : ''}${url ? ` url=${url}` : ''}${launchNote ? ` note=${launchNote.slice(0, 140)}` : ''}${err ? ` error=${err.slice(0, 120)}` : ''}`);
                if (sid) {
                    const actionText = ok
                        ? `浏览器步骤：${action} 执行成功（模式 ${mode}）${url ? `，目标 ${url}` : ''}`
                        : `浏览器步骤：${action} 执行失败（模式 ${mode}）${err ? `，错误：${err.slice(0, 120)}` : ''}`;
                    appendSubtaskTimeline(sid, actionText);
                }
                if (sid && !browserModeNotifiedRef.current[sid]) {
                    browserModeNotifiedRef.current[sid] = true;
                    const modeText = mode === 'headful' ? tr('可见窗口', 'visible window') : mode === 'headless' ? tr('无头后台', 'headless background') : mode;
                    addMessage('system', tr(`🌐 节点 #${sid} 已进入浏览器测试（${modeText}）`, `🌐 Node #${sid} entered browser testing (${modeText})`), 'Browser', '🌐', mode === 'headful' ? 'var(--green)' : 'var(--orange)');
                    if (launchNote) addMessage('system', tr(`⚠️ 浏览器模式降级：${launchNote}`, `⚠️ Browser mode fallback: ${launchNote}`), 'Browser', '⚠️', 'var(--orange)');
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
                updateNodeData(canvasNodeId, { nodeExecutionId: neId });
            }
            addConsoleLog(`[openclaw_node_ack] nodeExec=${neId} accepted=${String(payload.accepted)}`);

        } else if (t === 'evermind_dispatch_node') {
            // P1-2B: Auto-chained dispatch broadcast — update NE status to running
            const payload = (msg.payload || msg) as Record<string, unknown>;
            const neId = String(payload.nodeExecutionId || '');
            const runId = String(payload.runId || '');
            const taskId = String(payload.taskId || '');
            rememberPreviewRunId(payload.runId);
            addConsoleLog(`[evermind_dispatch_node] nodeExec=${neId} nodeKey=${String(payload.nodeKey || '')} autoChained=${String(payload.autoChained || false)}`);
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
            rememberPreviewRunId(payload.runId);
            // Pipe V1 metrics into canvas node data for AgentNode rendering
            const canvasNodeId = resolveCanvasNodeId(payload);
            if (canvasNodeId) {
                const updatePayload: Record<string, unknown> = { status };
                if (neId) updatePayload.nodeExecutionId = neId;
                if (payload.progress !== undefined) updatePayload.progress = Number(payload.progress);
                if (payload.tokensUsed !== undefined) updatePayload.tokensUsed = Number(payload.tokensUsed);
                if (payload.cost !== undefined) updatePayload.cost = Number(payload.cost);
                if (payload.costDelta !== undefined && !payload.cost) updatePayload.cost = Number(payload.costDelta);
                if (payload.assignedModel) updatePayload.assignedModel = String(payload.assignedModel);
                if (payload.partialOutputSummary) updatePayload.outputSummary = String(payload.partialOutputSummary);
                if (payload.startedAt) updatePayload.startedAt = Number(payload.startedAt);
                if (payload.endedAt) updatePayload.endedAt = Number(payload.endedAt);
                updateNodeData(canvasNodeId, updatePayload);
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
                const update: Record<string, unknown> = {};
                if (progressPct !== undefined) update.progress = progressPct;
                if (partialOutput) update.outputSummary = partialOutput;
                if (phase) update.phase = phase;
                if (toolCall) update.toolCall = toolCall;
                if (Object.keys(update).length) updateNodeData(canvasNodeId, update);
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
            // ── P0-1: Merge validation into canonical task state ──
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
            rememberPreviewRunId(payload.runId);
            addConsoleLog(`[openclaw_run_complete] runId=${String(payload.runId || '')} result=${finalResult}`);
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
                    status: success ? 'review' : 'executing',
                    latestSummary: String(payload.summary || payload.finalResult || ''),
                    latestRisk: Array.isArray(payload.risks) ? String(payload.risks[0] || '') : '',
                    updatedAt: updatedAt * 1000,
                    // P0-3: pass version from broadcast
                    ...(typeof payload._taskVersion === 'number' ? { version: payload._taskVersion } : {}),
                });
            }
        }
    }, [addMessage, appendSubtaskTimeline, clearPreviewFallbackTimer, difficulty, lang, messages, buildPlanNodes, updateNodeData, addReport, resetRunState, resolveCanvasNodeId, rememberPreviewRunId, onMergeTask, onMergeRun, onMergeNodeExecution]);

    const { connected, sendGoal, runWorkflow: wsRunWorkflow, stop, wsRef, send } = useWebSocket({ url: wsUrl, onMessage: onWSMessage });

    // ── Send goal ──
    const handleSendGoal = useCallback((goal: string) => {
        addMessage('user', goal, 'You', '👤');
        if (connected) {
            resetRunState();
            const recentHistory = messages
                .filter(m => m.role === 'user' || m.role === 'agent')
                .slice(-7)
                .map(m => ({ role: m.role, content: m.content.slice(0, 500) }));
            recentHistory.push({ role: 'user', content: goal.slice(0, 500) });
            sendGoal(goal, undefined, recentHistory, difficulty);
            addMessage('system', lang === 'zh' ? '🧠 已收到目标，正在规划执行...' : '🧠 Goal received — planning...', 'Evermind', '🧠');
        } else {
            addMessage('system',
                lang === 'zh' ? '🔴 后端未连接。请运行：<code>cd backend && python server.py</code>' : '🔴 Backend offline. Run: <code>cd backend && python server.py</code>',
                'System', '⚠️');
        }
    }, [connected, sendGoal, addMessage, messages, difficulty, lang, resetRunState]);

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

    return {
        running, previewUrl, previewRunId, canvasView, setCanvasView, connected, wsRef,
        handleSendGoal, handleRun, handleStop, setPreviewUrl,
        dispatchNode, cancelRunWS, resumeRunWS, rerunNodeWS, recheckStaleNodes,
    };
}
