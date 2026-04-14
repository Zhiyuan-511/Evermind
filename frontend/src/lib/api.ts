/* Evermind — REST API Client */

import type { ChatAttachment, RunReportRecord, RunSubtaskReport, SelfCheckItem, SkillLibraryRecord, TaskCard, TaskMode, TaskPriority, TaskStatus } from '@/lib/types';
import { MAX_CHAT_ATTACHMENTS, MAX_CHAT_ATTACHMENT_BYTES, normalizeChatAttachment } from '@/lib/chatAttachments';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
const DEFAULT_TIMEOUT_MS = 15_000;

const TASK_STATUSES: TaskStatus[] = ['backlog', 'planned', 'executing', 'review', 'selfcheck', 'done'];
const TASK_MODES: TaskMode[] = ['standard', 'pro', 'debug', 'review'];
const TASK_PRIORITIES: TaskPriority[] = ['low', 'medium', 'high', 'urgent'];

// In-flight deduplication for GET requests
const _inflight = new Map<string, Promise<unknown>>();

class ApiError extends Error {
    constructor(public status: number, message: string, public body?: unknown) {
        super(message);
        this.name = 'ApiError';
    }
}

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord | null {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    return value as JsonRecord;
}

function normalizeEpochMs(value: unknown, fallback = Date.now()): number {
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return fallback;
    return num < 10_000_000_000 ? Math.round(num * 1000) : Math.round(num);
}

function toEpochSeconds(value: unknown): number {
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return 0;
    return num >= 10_000_000_000 ? num / 1000 : num;
}

function normalizeStringList(value: unknown, itemLimit = 2000): string[] {
    const items = Array.isArray(value) ? value : typeof value === 'string' ? [value] : [];
    const seen = new Set<string>();
    return items.reduce<string[]>((acc, item) => {
        const text = String(item || '').trim().slice(0, itemLimit);
        if (!text || seen.has(text)) return acc;
        seen.add(text);
        acc.push(text);
        return acc;
    }, []);
}

function normalizeSelfcheckItems(value: unknown): SelfCheckItem[] {
    if (!Array.isArray(value)) return [];
    return value.reduce<SelfCheckItem[]>((acc, item) => {
        const record = asRecord(item);
        if (!record) return acc;
        const name = String(record.name || '').trim().slice(0, 200);
        const detail = String(record.detail || '').trim().slice(0, 1000);
        if (!name && !detail) return acc;
        acc.push({
            name,
            passed: Boolean(record.passed),
            detail,
        });
        return acc;
    }, []);
}

function normalizeSubtaskReport(raw: unknown, index: number): RunSubtaskReport {
    const value = asRecord(raw) || {};
    const startedAtRaw = value.startedAt ?? value.started_at;
    const endedAtRaw = value.endedAt ?? value.ended_at;
    const durationSeconds = Number(value.durationSeconds ?? value.duration_seconds);
    const timelineEventsRaw = value.timelineEvents ?? value.timeline_events;
    const workSummaryRaw = value.workSummary ?? value.work_summary;
    const filesCreatedRaw = value.filesCreated ?? value.files_created;

    return {
        id: String(value.id ?? index + 1),
        agent: String(value.agent || 'agent'),
        status: String(value.status || 'unknown'),
        retries: Math.max(0, Number(value.retries || 0)),
        task: String(value.task || '').slice(0, 1200),
        outputPreview: String(value.outputPreview ?? value.output_preview ?? '').slice(0, 2200),
        error: String(value.error || '').slice(0, 900),
        durationSeconds: Number.isFinite(durationSeconds) ? Math.max(0, durationSeconds) : undefined,
        startedAt: startedAtRaw ? normalizeEpochMs(startedAtRaw, 0) : undefined,
        endedAt: endedAtRaw ? normalizeEpochMs(endedAtRaw, 0) : undefined,
        timelineEvents: Array.isArray(timelineEventsRaw)
            ? timelineEventsRaw
                .filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
                .map((item) => item.slice(0, 260))
                .slice(0, 30)
            : undefined,
        workSummary: Array.isArray(workSummaryRaw)
            ? workSummaryRaw
                .filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
                .map((item) => item.slice(0, 260))
                .slice(0, 30)
            : undefined,
        filesCreated: normalizeStringList(filesCreatedRaw, 2000),
    };
}

function normalizeRunReport(raw: unknown): RunReportRecord | null {
    const value = asRecord(raw);
    if (!value) return null;

    const id = String(value.id || '').trim();
    const goal = String(value.goal || '').trim();
    if (!id || !goal) return null;

    const difficultyRaw = String(value.difficulty || 'standard');
    const difficulty: RunReportRecord['difficulty'] = difficultyRaw === 'simple' || difficultyRaw === 'pro'
        ? difficultyRaw
        : 'standard';
    const taskId = String(value.taskId ?? value.task_id ?? '').trim();
    const previewUrl = String(value.previewUrl ?? value.preview_url ?? '').trim();
    const subtasksRaw = Array.isArray(value.subtasks) ? value.subtasks : [];

    return {
        id,
        taskId: taskId || undefined,
        runId: String(value.runId ?? value.run_id ?? '').trim() || undefined,
        createdAt: normalizeEpochMs(value.createdAt ?? value.created_at),
        goal: goal.slice(0, 1200),
        difficulty,
        success: Boolean(value.success),
        totalSubtasks: Math.max(0, Number(value.totalSubtasks ?? value.total_subtasks ?? 0)),
        completed: Math.max(0, Number(value.completed || 0)),
        failed: Math.max(0, Number(value.failed || 0)),
        totalRetries: Math.max(0, Number(value.totalRetries ?? value.total_retries ?? 0)),
        durationSeconds: Math.max(0, Number(value.durationSeconds ?? value.duration_seconds ?? 0)),
        previewUrl: previewUrl || undefined,
        subtasks: subtasksRaw.slice(0, 30).map((item, index) => normalizeSubtaskReport(item, index)),
    };
}

function normalizeTaskCard(raw: unknown): TaskCard | null {
    const value = asRecord(raw);
    if (!value) return null;

    const id = String(value.id || '').trim();
    const title = String(value.title || '').trim();
    if (!id || !title) return null;

    const statusRaw = String(value.status || 'backlog');
    const modeRaw = String(value.mode || 'standard');
    const priorityRaw = String(value.priority || 'medium');
    const reportsRaw = Array.isArray(value.reports) ? value.reports : [];

    return {
        id,
        title: title.slice(0, 200),
        description: String(value.description || '').slice(0, 2000),
        status: TASK_STATUSES.includes(statusRaw as TaskStatus) ? statusRaw as TaskStatus : 'backlog',
        mode: TASK_MODES.includes(modeRaw as TaskMode) ? modeRaw as TaskMode : 'standard',
        owner: String(value.owner || '').slice(0, 100),
        progress: Math.max(0, Math.min(100, Number(value.progress || 0))),
        priority: TASK_PRIORITIES.includes(priorityRaw as TaskPriority) ? priorityRaw as TaskPriority : 'medium',
        createdAt: normalizeEpochMs(value.createdAt ?? value.created_at),
        updatedAt: normalizeEpochMs(value.updatedAt ?? value.updated_at),
        version: Number.isFinite(Number(value.version)) ? Math.max(0, Number(value.version)) : undefined,
        runIds: normalizeStringList(value.runIds ?? value.run_ids, 120),
        relatedFiles: normalizeStringList(value.relatedFiles ?? value.related_files),
        latestSummary: String(value.latestSummary ?? value.latest_summary ?? '').slice(0, 1000),
        latestRisk: String(value.latestRisk ?? value.latest_risk ?? '').slice(0, 500),
        reviewVerdict: String(value.reviewVerdict ?? value.review_verdict ?? '').slice(0, 40),
        reviewIssues: normalizeStringList(value.reviewIssues ?? value.review_issues, 500),
        selfcheckItems: normalizeSelfcheckItems(value.selfcheckItems ?? value.selfcheck_items),
        sessionId: String(value.sessionId ?? value.session_id ?? ''),
        reports: reportsRaw
            .map((item) => normalizeRunReport(item))
            .filter((item): item is RunReportRecord => !!item),
    };
}

function serializeTaskPayload(data: Partial<TaskCard>): Record<string, unknown> {
    const payload: Record<string, unknown> = { ...data };
    if ('createdAt' in data) payload.created_at = toEpochSeconds(data.createdAt);
    if ('updatedAt' in data) payload.updated_at = toEpochSeconds(data.updatedAt);
    if ('runIds' in data) payload.run_ids = data.runIds;
    if ('relatedFiles' in data) payload.related_files = data.relatedFiles;
    if ('latestSummary' in data) payload.latest_summary = data.latestSummary;
    if ('latestRisk' in data) payload.latest_risk = data.latestRisk;
    if ('reviewVerdict' in data) payload.review_verdict = data.reviewVerdict;
    if ('reviewIssues' in data) payload.review_issues = data.reviewIssues;
    if ('selfcheckItems' in data) payload.selfcheck_items = data.selfcheckItems;
    return payload;
}

function serializeReportPayload(data: Record<string, unknown>): Record<string, unknown> {
    const payload: Record<string, unknown> = { ...data };
    if ('taskId' in data) payload.task_id = data.taskId;
    if ('runId' in data) payload.run_id = data.runId;
    if ('createdAt' in data) payload.created_at = toEpochSeconds(data.createdAt);
    if ('totalSubtasks' in data) payload.total_subtasks = data.totalSubtasks;
    if ('totalRetries' in data) payload.total_retries = data.totalRetries;
    if ('durationSeconds' in data) payload.duration_seconds = data.durationSeconds;
    if ('previewUrl' in data) payload.preview_url = data.previewUrl;
    if (Array.isArray(data.subtasks)) {
        payload.subtasks = data.subtasks.map((item) => {
            const record = asRecord(item) || {};
            return {
                ...record,
                output_preview: record.outputPreview ?? record.output_preview ?? '',
                files_created: record.filesCreated ?? record.files_created ?? [],
                work_summary: record.workSummary ?? record.work_summary ?? [],
                duration_seconds: record.durationSeconds ?? record.duration_seconds ?? 0,
                started_at: record.startedAt ? toEpochSeconds(record.startedAt) : record.started_at ?? 0,
                ended_at: record.endedAt ? toEpochSeconds(record.endedAt) : record.ended_at ?? 0,
            };
        });
    }
    return payload;
}

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
    const method = (options?.method ?? 'GET').toUpperCase();
    const dedupeKey = method === 'GET' ? `GET:${path}` : '';

    if (dedupeKey) {
        const existing = _inflight.get(dedupeKey);
        if (existing) return existing as Promise<T>;
    }

    const promise = (async () => {
        const res = await fetch(`${API_BASE}${path}`, {
            headers: { 'Content-Type': 'application/json', ...options?.headers },
            signal: options?.signal ?? AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
            ...options,
        });
        if (!res.ok) {
            let body: unknown;
            try { body = await res.json(); } catch { /* ignore */ }
            const msg = (body && typeof body === 'object' && 'error' in body)
                ? String((body as Record<string, unknown>).error)
                : `API error: ${res.status}`;
            throw new ApiError(res.status, msg, body);
        }
        return res.json() as Promise<T>;
    })();

    if (dedupeKey) {
        _inflight.set(dedupeKey, promise);
        promise.finally(() => _inflight.delete(dedupeKey));
    }

    return promise;
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
    const bytes = new Uint8Array(buffer);
    const chunkSize = 0x8000;
    let binary = '';
    for (let index = 0; index < bytes.length; index += chunkSize) {
        const chunk = bytes.subarray(index, index + chunkSize);
        binary += String.fromCharCode(...chunk);
    }
    return btoa(binary);
}

interface ApiRequestOptions {
    signal?: AbortSignal;
}

// Health
export const getHealth = () => apiFetch<{ status: string; plugins_loaded: number; clients_connected: number }>('/api/health');

// Skills
export const listSkills = () => apiFetch<{
    skills: SkillLibraryRecord[];
    counts: { total: number; builtin: number; community: number };
    community_install_enabled: boolean;
}>('/api/skills');

export const installSkill = (data: {
    source_url: string;
    name?: string;
    title?: string;
    summary?: string;
    category?: string;
    node_types?: string[];
    keywords?: string[];
    tags?: string[];
}) => apiFetch<{ skill: SkillLibraryRecord }>('/api/skills/install', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
});

export const deleteSkill = (name: string) => apiFetch<{ success: boolean; skill_name: string }>(`/api/skills/${encodeURIComponent(name)}`, {
    method: 'DELETE',
});

export const getOpenClawGuide = () => apiFetch<{
    guide: string;
    mcp_config: Record<string, unknown>;
    ws_url: string;
    api_base: string;
    guide_url?: string;
    deep_links?: {
        open_app?: string;
        run_goal_template?: string;
    };
}>('/api/openclaw-guide');

export const uploadChatAttachments = async (
    sessionId: string,
    files: File[],
): Promise<{ attachments: ChatAttachment[]; rejected: string[] }> => {
    const selected = Array.isArray(files) ? files.slice(0, MAX_CHAT_ATTACHMENTS) : [];
    const rejected: string[] = [];
    const accepted = selected.filter((file) => {
        if (file.size <= MAX_CHAT_ATTACHMENT_BYTES) return true;
        rejected.push(`${file.name}: exceeds ${Math.round(MAX_CHAT_ATTACHMENT_BYTES / (1024 * 1024))}MB`);
        return false;
    });
    if (accepted.length === 0) return { attachments: [], rejected };

    const payloadFiles = await Promise.all(accepted.map(async (file) => ({
        name: file.name,
        mime_type: file.type || 'application/octet-stream',
        size: file.size,
        content_base64: arrayBufferToBase64(await file.arrayBuffer()),
    })));

    const response = await fetch(`${API_BASE}/api/chat/attachments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            session_id: String(sessionId || '').trim(),
            files: payloadFiles,
        }),
        signal: AbortSignal.timeout(60_000),
    });
    if (!response.ok) {
        let body: unknown;
        try { body = await response.json(); } catch { /* ignore */ }
        const msg = (body && typeof body === 'object' && 'error' in body)
            ? String((body as Record<string, unknown>).error)
            : `Attachment upload failed: ${response.status}`;
        throw new ApiError(response.status, msg, body);
    }
    const data = await response.json() as {
        attachments?: unknown[];
        rejected?: Array<{ name?: string; error?: string }>;
    };
    const attachments = (Array.isArray(data.attachments) ? data.attachments : [])
        .map((item) => normalizeChatAttachment(item))
        .filter((item): item is ChatAttachment => !!item);
    const rejectedFromServer = Array.isArray(data.rejected)
        ? data.rejected.map((item) => `${String(item?.name || 'attachment')}: ${String(item?.error || 'rejected')}`)
        : [];
    return { attachments, rejected: [...rejected, ...rejectedFromServer] };
};

// Models
export const getModels = () => apiFetch<{ models: Array<{ id: string; provider: string; supports_tools: boolean }> }>('/api/models');

// Plugins
export const getPlugins = () => apiFetch<{ plugins: Array<{ name: string; display_name: string; description: string; icon: string }> }>('/api/plugins');
export const getPluginDefaults = () => apiFetch<{ defaults: Record<string, string[]> }>('/api/plugins/defaults');

// Workflows (read-only — mutation APIs removed in v3.5 cleanup)
export const getWorkflows = () => apiFetch<{ workflows: unknown[] }>('/api/workflows');

// Tasks
export const getTasks = async (options?: ApiRequestOptions & { sessionId?: string }) => {
    const url = options?.sessionId ? `/api/tasks?sessionId=${encodeURIComponent(options.sessionId)}` : '/api/tasks';
    const result = await apiFetch<{ tasks: unknown[] }>(url, { signal: options?.signal });
    return {
        tasks: (Array.isArray(result.tasks) ? result.tasks : [])
            .map((item) => normalizeTaskCard(item))
            .filter((item): item is TaskCard => !!item),
    };
};

// G4: Board summary — tasks pre-joined with latest run + active node label
export interface BoardSummaryTask extends TaskCard {
    latestRun?: RunRecord | null;
    activeNodeLabel?: string;
    activeNodeLabels?: string[];
}
export const getBoardSummary = async (options?: ApiRequestOptions & { sessionId?: string }): Promise<{ tasks: BoardSummaryTask[] }> => {
    const url = options?.sessionId ? `/api/board-summary?sessionId=${encodeURIComponent(options.sessionId)}` : '/api/board-summary';
    const result = await apiFetch<{ tasks: unknown[] }>(url, { signal: options?.signal });
    return {
        tasks: (Array.isArray(result.tasks) ? result.tasks : []).reduce<BoardSummaryTask[]>((acc, item) => {
            const task = normalizeTaskCard(item);
            if (!task) return acc;
            const raw = asRecord(item) || {};
            acc.push({
                ...task,
                latestRun: asRecord(raw.latestRun) as unknown as RunRecord | null,
                activeNodeLabel: String(raw.activeNodeLabel || ''),
                activeNodeLabels: Array.isArray(raw.activeNodeLabels)
                    ? raw.activeNodeLabels.map(String).filter(Boolean)
                    : [],
            });
            return acc;
        }, []),
    };
};

export const deleteTasksBySession = async (sessionId: string): Promise<{ success: boolean; deleted: number }> => {
    return apiFetch<{ success: boolean; deleted: number }>(`/api/tasks/session/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE',
    });
};

export const createTask = async (data: Partial<TaskCard>) => {
    const result = await apiFetch<{ success: boolean; task: unknown }>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify(serializeTaskPayload(data)),
    });
    const task = normalizeTaskCard(result.task);
    if (!task) {
        throw new ApiError(500, 'Invalid task payload', result.task);
    }
    return {
        success: Boolean(result.success),
        task,
    };
};

export const getTaskById = async (id: string, options?: ApiRequestOptions) => {
    const result = await apiFetch<{ task: unknown }>(`/api/tasks/${id}`, { signal: options?.signal });
    const task = normalizeTaskCard(result.task);
    if (!task) {
        throw new ApiError(500, 'Invalid task payload', result.task);
    }
    return { task };
};

export const updateTaskApi = async (id: string, data: Partial<TaskCard>) => {
    const result = await apiFetch<{ success: boolean; task: unknown }>(`/api/tasks/${id}`, {
        method: 'PUT',
        body: JSON.stringify(serializeTaskPayload(data)),
    });
    const task = normalizeTaskCard(result.task);
    if (!task) {
        throw new ApiError(500, 'Invalid task payload', result.task);
    }
    return {
        success: Boolean(result.success),
        task,
    };
};

export const transitionTask = async (id: string, status: string) => {
    const result = await apiFetch<{ success: boolean; task?: unknown; error?: string }>(`/api/tasks/${id}/transition`, {
        method: 'POST',
        body: JSON.stringify({ status }),
    });
    return {
        success: Boolean(result.success),
        task: result.task ? normalizeTaskCard(result.task) : undefined,
        error: result.error,
    };
};

// Reports
export const getReports = async (taskId?: string) => {
    const result = await apiFetch<{ reports: unknown[] }>(`/api/reports${taskId ? `?task_id=${taskId}` : ''}`);
    return {
        reports: (Array.isArray(result.reports) ? result.reports : [])
            .map((item) => normalizeRunReport(item))
            .filter((item): item is RunReportRecord => !!item),
    };
};

export const saveReportApi = async (data: Record<string, unknown>) => {
    const result = await apiFetch<{ success: boolean; report: unknown }>('/api/reports', {
        method: 'POST',
        body: JSON.stringify(serializeReportPayload(data)),
    });
    const report = normalizeRunReport(result.report);
    if (!report) {
        throw new ApiError(500, 'Invalid report payload', result.report);
    }
    return {
        success: Boolean(result.success),
        report,
    };
};

export const getReportById = async (id: string) => {
    const result = await apiFetch<{ report: unknown }>(`/api/reports/${id}`);
    const report = normalizeRunReport(result.report);
    if (!report) {
        throw new ApiError(500, 'Invalid report payload', result.report);
    }
    return { report };
};

// ─────────────────────────────────────────
// V1 Run API
// ─────────────────────────────────────────
import type { RunRecord, NodeExecutionRecord, ArtifactRecord } from './types';

export const listRuns = async (taskId?: string, options?: ApiRequestOptions): Promise<{ runs: RunRecord[] }> => {
    const qs = taskId ? `?taskId=${encodeURIComponent(taskId)}` : '';
    return apiFetch(`/api/runs${qs}`, { signal: options?.signal });
};

export const createRun = async (data: Partial<RunRecord> & { task_id: string }): Promise<{ run: RunRecord }> => {
    return apiFetch('/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    });
};

export const getRun = async (runId: string, options?: ApiRequestOptions): Promise<{ run: RunRecord }> => {
    return apiFetch(`/api/runs/${runId}`, { signal: options?.signal });
};

export const updateRun = async (runId: string, data: Partial<RunRecord>): Promise<{ run: RunRecord }> => {
    return apiFetch(`/api/runs/${runId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    });
};

export const transitionRun = async (runId: string, status: string): Promise<{ success: boolean; run?: RunRecord; error?: string }> => {
    return apiFetch(`/api/runs/${runId}/transition`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
    });
};

export const cancelRun = async (runId: string): Promise<{ success: boolean; run?: RunRecord; cancelledNodes?: number }> => {
    return apiFetch(`/api/runs/${runId}/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
    });
};

// P2-A: Workflow Templates
export interface WorkflowTemplateSummary {
    id: string;
    label: string;
    description: string;
    nodeCount: number;
    nodeCountMin?: number;
    nodeCountMax?: number;
}

export const listWorkflowTemplates = async (): Promise<{ templates: WorkflowTemplateSummary[] }> => {
    return apiFetch('/api/workflow-templates');
};

// P2-B: One-shot Launch
export interface LaunchRunResult {
    success: boolean;
    run: RunRecord;
    task: TaskCard;
    nodeExecutions: NodeExecutionRecord[];
    firstDispatchNodeId: string | null;
    dispatchedNodeIds: string[];
    templateId: string;
}

export const launchRun = async (data: {
    task_id: string;
    template_id?: string;
    runtime?: string;
    timeout_seconds?: number;
    trigger_source?: string;
}): Promise<LaunchRunResult> => {
    const result = await apiFetch<{
        success: boolean;
        run: RunRecord;
        task: unknown;
        nodeExecutions: NodeExecutionRecord[];
        firstDispatchNodeId: string | null;
        dispatchedNodeIds?: unknown;
        templateId: string;
    }>('/api/runs/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    });
    const task = normalizeTaskCard(result.task);
    if (!task) {
        throw new ApiError(500, 'Invalid task payload', result.task);
    }
    return {
        ...result,
        task,
        dispatchedNodeIds: Array.isArray(result.dispatchedNodeIds)
            ? result.dispatchedNodeIds.map(String)
            : (result.firstDispatchNodeId ? [result.firstDispatchNodeId] : []),
    };
};

// ─────────────────────────────────────────
// V1 NodeExecution API
// ─────────────────────────────────────────
export const listNodeExecutions = async (runId?: string, options?: ApiRequestOptions): Promise<{ nodeExecutions: NodeExecutionRecord[] }> => {
    const qs = runId ? `?runId=${encodeURIComponent(runId)}` : '';
    return apiFetch(`/api/node-executions${qs}`, { signal: options?.signal });
};

export const getNodeExecution = async (nodeId: string, options?: ApiRequestOptions): Promise<{ nodeExecution: NodeExecutionRecord }> => {
    return apiFetch(`/api/node-executions/${nodeId}`, { signal: options?.signal });
};

export const retryNodeExecution = async (nodeId: string): Promise<{ nodeExecution: NodeExecutionRecord; retriedFrom: string }> => {
    return apiFetch(`/api/node-executions/${nodeId}/retry`, {
        method: 'POST',
    });
};

// ─────────────────────────────────────────
// V1 Artifact API
// ─────────────────────────────────────────
export const listArtifacts = async (runId?: string, nodeExecutionId?: string, options?: ApiRequestOptions): Promise<{ artifacts: ArtifactRecord[] }> => {
    const params = new URLSearchParams();
    if (runId) params.set('runId', runId);
    if (nodeExecutionId) params.set('nodeExecutionId', nodeExecutionId);
    const qs = params.toString() ? `?${params.toString()}` : '';
    return apiFetch(`/api/artifacts${qs}`, { signal: options?.signal });
};

export const saveArtifact = async (data: Partial<ArtifactRecord>): Promise<{ artifact: ArtifactRecord }> => {
    return apiFetch('/api/artifacts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    });
};

export const getArtifact = async (artifactId: string): Promise<{ artifact: ArtifactRecord }> => {
    return apiFetch(`/api/artifacts/${artifactId}`);
};

export { ApiError };
