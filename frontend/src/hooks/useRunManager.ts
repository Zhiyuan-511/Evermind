'use client';

import { useState, useCallback, useRef, useMemo, useEffect } from 'react';
import type { RunRecord, NodeExecutionRecord, RunStatus } from '@/lib/types';
import {
    listRuns,
    createRun as apiCreateRun,
    transitionRun as apiTransitionRun,
    getRun,
    listNodeExecutions,
    retryNodeExecution,
} from '@/lib/api';

// ── Types ──

export type RunMergePatch = Partial<RunRecord> & Pick<RunRecord, 'id'>;
export type NodeExecutionMergePatch = Partial<NodeExecutionRecord> & Pick<NodeExecutionRecord, 'id' | 'run_id'>;

export interface UseRunManagerOptions {
    /**
     * When set, runs are scoped to this task.
     * Changing taskId triggers an automatic re-fetch.
     */
    taskId: string | null;
    /**
     * If true, auto-poll runs for active tasks (every pollIntervalMs).
     * Default: false.
     */
    autoPoll?: boolean;
    /**
     * Polling interval in ms. Default: 8000.
     */
    pollIntervalMs?: number;
}

export interface UseRunManagerReturn {
    /** Runs for the current taskId scope. */
    runs: RunRecord[];
    /** Currently selected run (derived). */
    selectedRun: RunRecord | null;
    /** Latest run by started_at descending. */
    latestRun: RunRecord | null;
    /** Set the selected run by ID (null to deselect). Triggers nodeExecution fetch. */
    selectRun: (id: string | null) => void;
    /** NodeExecutions for the currently selected run. */
    nodeExecutions: NodeExecutionRecord[];
    /** Whether runs are being fetched. */
    loading: boolean;
    /** Whether nodeExecutions are being fetched. */
    nodesLoading: boolean;
    /** Last error message. */
    error: string | null;
    /** Fetch runs for the current taskId. */
    fetchRuns: () => Promise<void>;
    /** Create a new run for the current task. */
    createRun: (data?: Partial<RunRecord>) => Promise<RunRecord | null>;
    /** Transition a run to a new status. */
    transitionRun: (runId: string, status: RunStatus) => Promise<RunRecord | null>;
    /** Re-fetch a single run and merge it into the list. */
    refreshRun: (runId: string) => Promise<void>;
    /** Fetch nodeExecutions for a specific run. */
    fetchNodeExecutions: (runId: string) => Promise<NodeExecutionRecord[]>;
    /** Retry a failed node execution. Returns the new NodeExecution. */
    retryNode: (nodeId: string) => Promise<NodeExecutionRecord | null>;
    /** Get runs filtered by status. */
    runsByStatus: (status: RunStatus) => RunRecord[];
    /** Merge a run from an external source (e.g. WS). */
    mergeRun: (run: RunMergePatch) => void;
    /** Merge a nodeExecution from an external source (e.g. WS). */
    mergeNodeExecution: (ne: NodeExecutionMergePatch) => void;
}

// ── Helpers ──

function normalizeEpoch(value: number): number {
    if (!value) return 0;
    return value < 10_000_000_000 ? value * 1000 : value;
}

function hasComparableVersion(value: number | undefined): value is number {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/**
 * P0-3: Version-first comparison.
 * If both sides have a numeric version, use that (monotonic, no clock skew).
 * Otherwise fall back to normalized epoch timestamps.
 */
function isNewerOrEqual(
    incomingVersion: number | undefined,
    currentVersion: number | undefined,
    incomingUpdatedAt: number,
    currentUpdatedAt: number,
): boolean {
    if (hasComparableVersion(incomingVersion) && hasComparableVersion(currentVersion)) {
        return incomingVersion >= currentVersion;
    }
    return normalizeEpoch(incomingUpdatedAt) >= normalizeEpoch(currentUpdatedAt);
}

function canRollbackRun(current: RunRecord, snapshot: RunRecord, optimisticUpdatedAt: number): boolean {
    if (hasComparableVersion(current.version) && hasComparableVersion(snapshot.version)) {
        return current.version === snapshot.version;
    }
    return normalizeEpoch(current.updated_at) <= normalizeEpoch(optimisticUpdatedAt);
}

function sortRunsDesc(runs: RunRecord[]): RunRecord[] {
    return [...runs].sort((a, b) => normalizeEpoch(b.started_at) - normalizeEpoch(a.started_at));
}

function sortNodeExecutionsAsc(items: NodeExecutionRecord[]): NodeExecutionRecord[] {
    return [...items].sort((a, b) => normalizeEpoch(a.started_at || a.created_at) - normalizeEpoch(b.started_at || b.created_at));
}

function preserveEphemeralNodeFields(
    fetched: NodeExecutionRecord[],
    current: NodeExecutionRecord[],
): NodeExecutionRecord[] {
    const currentById = new Map(current.map((node) => [node.id, node]));
    return fetched.map((node) => {
        const existing = currentById.get(node.id);
        if (!existing) return node;

        const next = { ...node };
        const shouldPreserveRuntimeProgress =
            node.status === 'running' &&
            existing.status === 'running';

        if (shouldPreserveRuntimeProgress && typeof node.progress !== 'number' && typeof existing.progress === 'number') {
            next.progress = existing.progress;
        }
        if (shouldPreserveRuntimeProgress && !node.phase && existing.phase) {
            next.phase = existing.phase;
        }
        return next;
    });
}

function buildRunFromPatch(run: RunMergePatch, fallbackTaskId: string | null): RunRecord {
    const now = Date.now() / 1000;
    return {
        id: run.id,
        task_id: run.task_id || fallbackTaskId || '',
        status: run.status || 'queued',
        trigger_source: run.trigger_source || 'ui',
        runtime: run.runtime || 'local',
        workflow_template_id: run.workflow_template_id || '',
        current_node_execution_id: run.current_node_execution_id || '',
        active_node_execution_ids: Array.isArray(run.active_node_execution_ids) ? run.active_node_execution_ids : [],
        started_at: run.started_at || 0,
        ended_at: run.ended_at || 0,
        total_tokens: run.total_tokens || 0,
        total_cost: run.total_cost || 0,
        summary: run.summary || '',
        risks: run.risks || [],
        node_execution_ids: run.node_execution_ids || [],
        created_at: run.created_at || now,
        updated_at: run.updated_at || now,
        version: run.version,
        timeout_seconds: run.timeout_seconds || 0,
    };
}

function buildNodeExecutionFromPatch(ne: NodeExecutionMergePatch): NodeExecutionRecord {
    const now = Date.now() / 1000;
    return {
        id: ne.id,
        run_id: ne.run_id,
        node_key: ne.node_key || '',
        node_label: ne.node_label || '',
        retried_from_id: ne.retried_from_id || '',
        status: ne.status || 'queued',
        assigned_model: ne.assigned_model || '',
        assigned_provider: ne.assigned_provider || '',
        input_summary: ne.input_summary || '',
        output_summary: ne.output_summary || '',
        error_message: ne.error_message || '',
        retry_count: ne.retry_count || 0,
        tokens_used: ne.tokens_used || 0,
        cost: ne.cost || 0,
        started_at: ne.started_at || 0,
        ended_at: ne.ended_at || 0,
        artifact_ids: ne.artifact_ids || [],
        created_at: ne.created_at || now,
        updated_at: ne.updated_at || now,
        progress: ne.progress,
        phase: ne.phase || '',
        loaded_skills: Array.isArray(ne.loaded_skills) ? ne.loaded_skills : [],
        activity_log: Array.isArray(ne.activity_log) ? ne.activity_log : [],
        reference_urls: Array.isArray(ne.reference_urls) ? ne.reference_urls : [],
        current_action: ne.current_action || '',
        work_summary: Array.isArray(ne.work_summary) ? ne.work_summary : [],
        tool_call_stats: ne.tool_call_stats || {},
        report_artifact_ids: Array.isArray(ne.report_artifact_ids) ? ne.report_artifact_ids : [],
        handoff_artifact_ids: Array.isArray(ne.handoff_artifact_ids) ? ne.handoff_artifact_ids : [],
        dossier_artifact_id: ne.dossier_artifact_id || '',
        summary_artifact_id: ne.summary_artifact_id || '',
        blocking_reason: ne.blocking_reason || '',
        latest_review_decision: ne.latest_review_decision || '',
        latest_review_report_artifact_id: ne.latest_review_report_artifact_id || '',
        latest_merge_manifest_artifact_id: ne.latest_merge_manifest_artifact_id || '',
        latest_deployment_receipt_artifact_id: ne.latest_deployment_receipt_artifact_id || '',
        version: ne.version,
        timeout_seconds: ne.timeout_seconds || 0,
        depends_on_keys: Array.isArray(ne.depends_on_keys) ? ne.depends_on_keys : [],
    };
}

// ── Hook ──

export function useRunManager({
    taskId,
    autoPoll = false,
    pollIntervalMs = 8000,
}: UseRunManagerOptions): UseRunManagerReturn {
    const [runs, setRuns] = useState<RunRecord[]>([]);
    const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
    const [nodeExecutions, setNodeExecutions] = useState<NodeExecutionRecord[]>([]);
    const [loading, setLoading] = useState(false);
    const [nodesLoading, setNodesLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // AbortControllers for cancellation
    const runsAbortRef = useRef<AbortController | null>(null);
    const nodesAbortRef = useRef<AbortController | null>(null);

    // Track taskId to reset on change
    const prevTaskIdRef = useRef<string | null>(null);

    // §FIX-4: Track known run IDs so mergeNodeExecution can validate NEs without runs in closure
    const knownRunIdsRef = useRef<Set<string>>(new Set());

    // Cleanup on unmount
    useEffect(() => {
        return () => {
            runsAbortRef.current?.abort();
            nodesAbortRef.current?.abort();
        };
    }, []);

    // ── Derived state ──

    const selectedRun = useMemo(() => {
        if (!selectedRunId) return null;
        return runs.find((r) => r.id === selectedRunId) ?? null;
    }, [runs, selectedRunId]);

    const latestRun = useMemo(() => {
        if (runs.length === 0) return null;
        return sortRunsDesc(runs)[0] ?? null;
    }, [runs]);

    const runsByStatus = useCallback(
        (status: RunStatus): RunRecord[] =>
            sortRunsDesc(runs.filter((r) => r.status === status)),
        [runs],
    );

    // ── Fetch runs ──

    const fetchRuns = useCallback(async () => {
        if (!taskId) {
            setRuns([]);
            return;
        }

        runsAbortRef.current?.abort();
        const controller = new AbortController();
        runsAbortRef.current = controller;

        setLoading(true);
        setError(null);

        try {
            const { runs: fetched } = await listRuns(taskId, { signal: controller.signal });
            if (controller.signal.aborted) return;
            const sorted = sortRunsDesc(fetched || []);
            setRuns(sorted);
            // §FIX-4: Track fetched run IDs so mergeNodeExecution can validate against them
            for (const r of sorted) knownRunIdsRef.current.add(r.id);
        } catch (err: unknown) {
            if (controller.signal.aborted) return;
            if (err instanceof Error && err.name === 'AbortError') return;
            setError(err instanceof Error ? err.message : 'Failed to fetch runs');
        } finally {
            if (!controller.signal.aborted) {
                setLoading(false);
            }
        }
    }, [taskId]);

    // ── Auto-fetch when taskId changes ──

    useEffect(() => {
        if (taskId !== prevTaskIdRef.current) {
            prevTaskIdRef.current = taskId;
            runsAbortRef.current?.abort();
            nodesAbortRef.current?.abort();
            // Reset state when task scope changes
            setSelectedRunId(null);
            setNodeExecutions([]);
            // v7.7: was leaving `runs` populated when switching from task A
            // to task B with explicit fetch — the stale A-runs lingered until
            // fetchRuns resolved. Combined with mergeNodeExecution's
            // knownRunIds fallback, this leaked A's WS NE updates into B's
            // pipeline panel ("看到的是上个 task 的事件"). Now: always clear
            // runs synchronously on task switch; fetchRuns repopulates after.
            setRuns([]);
            knownRunIdsRef.current.clear();
            setError(null);
            if (taskId) {
                fetchRuns();
            }
        }
    }, [taskId, fetchRuns]);

    useEffect(() => {
        if (runs.length === 0) {
            if (selectedRunId) {
                setSelectedRunId(null);
                setNodeExecutions([]);
            }
            return;
        }

        const selectedExists = selectedRunId ? runs.some((run) => run.id === selectedRunId) : false;
        if (selectedRunId && !selectedExists) {
            setSelectedRunId(null);
            setNodeExecutions([]);
        }
    }, [runs, selectedRunId]);

    // ── Fetch nodeExecutions ──

    const fetchNodeExecutions = useCallback(async (runId: string): Promise<NodeExecutionRecord[]> => {
        nodesAbortRef.current?.abort();
        const controller = new AbortController();
        nodesAbortRef.current = controller;

        setNodesLoading(true);

        try {
            const { nodeExecutions: fetched } = await listNodeExecutions(runId, { signal: controller.signal });
            if (controller.signal.aborted) return [];
            const sorted = sortNodeExecutionsAsc(fetched || []);
            let merged: NodeExecutionRecord[] = sorted;
            setNodeExecutions((prev) => {
                merged = sortNodeExecutionsAsc(preserveEphemeralNodeFields(sorted, prev));
                return merged;
            });
            return merged;
        } catch (err: unknown) {
            if (controller.signal.aborted) return [];
            if (err instanceof Error && err.name === 'AbortError') return [];
            setError(err instanceof Error ? err.message : 'Failed to fetch node executions');
            return [];
        } finally {
            if (!controller.signal.aborted) {
                setNodesLoading(false);
            }
        }
    }, []);

    // ── Auto-poll ──

    useEffect(() => {
        if (!autoPoll || !taskId) return;

        const hasActiveRun = runs.some((r) =>
            ['queued', 'running', 'waiting_review', 'waiting_selfcheck'].includes(r.status),
        );
        if (!hasActiveRun) return;

        const timer = setInterval(() => {
            void fetchRuns();
            if (selectedRunId) {
                void fetchNodeExecutions(selectedRunId);
            }
        }, pollIntervalMs);

        return () => clearInterval(timer);
    }, [autoPoll, taskId, runs, fetchRuns, fetchNodeExecutions, pollIntervalMs, selectedRunId]);

    // ── Select run (auto-loads nodeExecutions) ──

    const selectRun = useCallback(
        (id: string | null) => {
            setSelectedRunId(id);
            if (id) {
                fetchNodeExecutions(id);
            } else {
                setNodeExecutions([]);
            }
        },
        [fetchNodeExecutions],
    );

    // ── Create run ──

    const createRun = useCallback(
        async (data?: Partial<RunRecord>): Promise<RunRecord | null> => {
            if (!taskId) {
                setError('No task selected');
                return null;
            }

            setError(null);

            try {
                const { run } = await apiCreateRun({
                    task_id: taskId,
                    ...data,
                });
                // Prepend to list
                setRuns((prev) => sortRunsDesc([run, ...prev]));
                setSelectedRunId(run.id);
                setNodeExecutions([]);
                return run;
            } catch (err: unknown) {
                setError(err instanceof Error ? err.message : 'Failed to create run');
                return null;
            }
        },
        [taskId],
    );

    // ── Transition run (optimistic) ──

    const transitionRun = useCallback(async (runId: string, status: RunStatus): Promise<RunRecord | null> => {
        let snapshot: RunRecord | undefined;
        const optimisticUpdatedAt = Date.now() / 1000;

        setRuns((prev) =>
            prev.map((r) => {
                if (r.id !== runId) return r;
                snapshot = r;
                return { ...r, status, updated_at: optimisticUpdatedAt };
            }),
        );
        setError(null);

        try {
            const result = await apiTransitionRun(runId, status);
            if (result.success && result.run) {
                setRuns((prev) => prev.map((r) => (r.id === runId ? result.run! : r)));
                return result.run;
            }
            // Server rejected — rollback
            if (snapshot) {
                setRuns((prev) => prev.map((r) => {
                    if (r.id !== runId) return r;
                    return canRollbackRun(r, snapshot!, optimisticUpdatedAt) ? snapshot! : r;
                }));
            }
            setError(result.error || 'Transition rejected');
            return null;
        } catch (err: unknown) {
            if (snapshot) {
                setRuns((prev) => prev.map((r) => {
                    if (r.id !== runId) return r;
                    return canRollbackRun(r, snapshot!, optimisticUpdatedAt) ? snapshot! : r;
                }));
            }
            setError(err instanceof Error ? err.message : 'Failed to transition run');
            return null;
        }
    }, []);

    // ── Refresh single run ──

    const refreshRun = useCallback(async (runId: string) => {
        try {
            const controller = new AbortController();
            const { run } = await getRun(runId, { signal: controller.signal });
            setRuns((prev) => {
                if (taskId && run.task_id !== taskId) return prev;
                const idx = prev.findIndex((r) => r.id === runId);
                if (idx >= 0) {
                    const next = [...prev];
                    next[idx] = run;
                    return sortRunsDesc(next);
                }
                return sortRunsDesc([run, ...prev]);
            });
        } catch {
            // Silently ignore
        }
    }, [taskId]);

    // ── Retry node ──

    const retryNode = useCallback(
        async (nodeId: string): Promise<NodeExecutionRecord | null> => {
            try {
                const { nodeExecution } = await retryNodeExecution(nodeId);
                // Refresh the nodeExecutions list for the current run
                if (selectedRunId) {
                    await fetchNodeExecutions(selectedRunId);
                }
                return nodeExecution;
            } catch (err: unknown) {
                setError(err instanceof Error ? err.message : 'Failed to retry node');
                return null;
            }
        },
        [selectedRunId, fetchNodeExecutions],
    );

    // ── Merge from external source (WS) ──

    const mergeRun = useCallback((run: RunMergePatch) => {
        let shouldAutoSelect = false;
        setRuns((prev) => {
            const idx = prev.findIndex((r) => r.id === run.id);
            if (idx >= 0) {
                const current = prev[idx];
                const incomingUpdatedAt = run.updated_at ?? current.updated_at;
                // P0-3: version-first comparison
                if (isNewerOrEqual(run.version, current.version, incomingUpdatedAt, current.updated_at)) {
                    const next = [...prev];
                    next[idx] = {
                        ...current,
                        ...run,
                        task_id: run.task_id || current.task_id,
                        updated_at: incomingUpdatedAt,
                        version: hasComparableVersion(run.version)
                            ? run.version
                            : current.version,
                    };
                    return sortRunsDesc(next);
                }
                return prev;
            }
            // §FIX-1: Accept runs even when no task is selected (taskId is null).
            // v7.7: tightened — when taskId IS set, the incoming run must
            // either name the same task or omit task_id (legacy events).
            // Previous logic let cross-task runs slip in when their task_id
            // happened to be empty/missing, which leaked A's events into B's
            // panel after a task switch.
            const inferredTaskId = run.task_id || '';
            if (taskId && inferredTaskId && inferredTaskId !== taskId) return prev;
            shouldAutoSelect = true;
            return sortRunsDesc([buildRunFromPatch(run, taskId), ...prev]);
        });
        // §FIX-4: Track the newly inserted run so mergeNodeExecution can validate it
        knownRunIdsRef.current.add(run.id);
        // §FIX-2: Auto-select the newly merged run if nothing is currently selected.
        if (shouldAutoSelect) {
            setSelectedRunId((prev) => {
                if (prev) return prev; // keep existing selection
                return run.id;
            });
        }
    }, [taskId]);

    // §FIX-3 (v7.7 hardened): accept NEs only when the run is in the current
    // task scope. Was using `knownRunIdsRef` as fallback when selectedRunId
    // is null — but that ref accumulated old runs across task switches and
    // never cleaned them, so cross-task NE updates leaked into the active
    // pipeline panel. The current `runs` array IS task-scoped (cleared on
    // task switch in the effect at line ~298), so use it as the truth source.
    const mergeNodeExecution = useCallback((ne: NodeExecutionMergePatch) => {
        const matchesSelected = selectedRunId && ne.run_id === selectedRunId;
        // When no run is selected yet, accept NEs only from runs that are
        // part of the current task's `runs` list (task-scoped).
        const knownInScope = !selectedRunId && runs.some((r) => r.id === ne.run_id);
        if (!matchesSelected && !knownInScope) return;
        setNodeExecutions((prev) => {
            const idx = prev.findIndex((n) => n.id === ne.id);
            if (idx >= 0) {
                const current = prev[idx];
                const incomingUpdatedAt = ne.updated_at ?? current.updated_at;
                // P0-3: version-first comparison
                if (isNewerOrEqual(ne.version, current.version, incomingUpdatedAt, current.updated_at)) {
                    const next = [...prev];
                    next[idx] = {
                        ...current,
                        ...ne,
                        updated_at: incomingUpdatedAt,
                        version: hasComparableVersion(ne.version)
                            ? ne.version
                            : current.version,
                    };
                    return sortNodeExecutionsAsc(next);
                }
                return prev;
            }
            // New — insert in chronological order
            return sortNodeExecutionsAsc([...prev, buildNodeExecutionFromPatch(ne)]);
        });
    }, [selectedRunId, runs]);

    return {
        runs,
        selectedRun,
        latestRun,
        selectRun,
        nodeExecutions,
        loading,
        nodesLoading,
        error,
        fetchRuns,
        createRun,
        transitionRun,
        refreshRun,
        fetchNodeExecutions,
        retryNode,
        runsByStatus,
        mergeRun,
        mergeNodeExecution,
    };
}
