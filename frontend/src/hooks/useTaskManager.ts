'use client';

import { useState, useCallback, useRef, useMemo, useEffect } from 'react';
import type { TaskCard, TaskStatus } from '@/lib/types';
import { getTasks, createTask as apiCreateTask, updateTaskApi, transitionTask as apiTransitionTask, getTaskById } from '@/lib/api';

// ── Types ──

export type TaskMergePatch = Partial<TaskCard> & Pick<TaskCard, 'id'>;

export interface UseTaskManagerReturn {
    /** All tasks (canonical list). */
    tasks: TaskCard[];
    /** Currently selected task (derived). */
    selectedTask: TaskCard | null;
    /** Set the selected task by ID (null to deselect). */
    selectTask: (id: string | null) => void;
    /** Whether initial fetch is in progress. */
    loading: boolean;
    /** Last error message, or null. */
    error: string | null;
    /** Fetch (or re-fetch) all tasks from the backend. */
    fetchTasks: () => Promise<void>;
    /** Create a new task. Returns the created task. */
    createTask: (data: Partial<TaskCard>) => Promise<TaskCard | null>;
    /** Update an existing task. Returns the updated task. */
    updateTask: (id: string, data: Partial<TaskCard>) => Promise<TaskCard | null>;
    /** Transition a task to a new status. Returns the updated task. */
    transitionTask: (id: string, status: TaskStatus) => Promise<TaskCard | null>;
    /** Re-fetch a single task from the backend and merge it into the list. */
    refreshTask: (id: string) => Promise<void>;
    /** Get tasks filtered by status, sorted by updatedAt descending. */
    tasksByStatus: (status: TaskStatus) => TaskCard[];
    /** Merge an externally-provided task into the local list (e.g. from WS). */
    mergeTask: (task: TaskMergePatch) => void;
}

function hasComparableVersion(value: number | undefined): value is number {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function isTaskPatchNewerOrEqual(task: TaskMergePatch, current: TaskCard): boolean {
    const incomingVersion = task.version;
    const currentVersion = current.version;
    if (hasComparableVersion(incomingVersion) && hasComparableVersion(currentVersion)) {
        return incomingVersion >= currentVersion;
    }
    const incomingUpdatedAt = task.updatedAt ?? current.updatedAt;
    return incomingUpdatedAt >= current.updatedAt;
}

function canRollbackTask(current: TaskCard, snapshot: TaskCard, optimisticUpdatedAt: number): boolean {
    if (hasComparableVersion(current.version) && hasComparableVersion(snapshot.version)) {
        return current.version === snapshot.version;
    }
    return current.updatedAt <= optimisticUpdatedAt;
}

function sortTasksDesc(items: TaskCard[]): TaskCard[] {
    return [...items].sort((a, b) => b.updatedAt - a.updatedAt);
}

function buildTaskFromPatch(task: TaskMergePatch): TaskCard {
    const now = Date.now();
    return {
        id: task.id,
        title: task.title || '',
        description: task.description || '',
        status: task.status || 'backlog',
        mode: task.mode || 'standard',
        owner: task.owner || '',
        progress: task.progress || 0,
        priority: task.priority || 'medium',
        createdAt: task.createdAt || now,
        updatedAt: task.updatedAt || now,
        version: task.version,
        runIds: task.runIds || [],
        relatedFiles: task.relatedFiles || [],
        latestSummary: task.latestSummary || '',
        latestRisk: task.latestRisk || '',
        reviewVerdict: task.reviewVerdict || '',
        reviewIssues: task.reviewIssues || [],
        selfcheckItems: task.selfcheckItems || [],
    };
}

// ── Hook ──

export function useTaskManager(): UseTaskManagerReturn {
    const [tasks, setTasks] = useState<TaskCard[]>([]);
    const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // AbortController for fetch cancellation
    const listAbortRef = useRef<AbortController | null>(null);
    const refreshAbortRef = useRef<AbortController | null>(null);

    // Cleanup on unmount
    useEffect(() => {
        return () => {
            listAbortRef.current?.abort();
            refreshAbortRef.current?.abort();
        };
    }, []);

    // ── Derived state ──

    const selectedTask = useMemo(() => {
        if (!selectedTaskId) return null;
        return tasks.find((t) => t.id === selectedTaskId) ?? null;
    }, [tasks, selectedTaskId]);

    useEffect(() => {
        if (selectedTaskId && !tasks.some((task) => task.id === selectedTaskId)) {
            setSelectedTaskId(null);
        }
    }, [selectedTaskId, tasks]);

    const tasksByStatus = useCallback(
        (status: TaskStatus): TaskCard[] =>
            tasks
                .filter((t) => t.status === status)
                .sort((a, b) => b.updatedAt - a.updatedAt),
        [tasks],
    );

    // ── Fetch all tasks ──

    const fetchTasks = useCallback(async () => {
        // Cancel any in-flight fetch
        listAbortRef.current?.abort();
        const controller = new AbortController();
        listAbortRef.current = controller;

        setLoading(true);
        setError(null);

        try {
            const { tasks: fetched } = await getTasks({ signal: controller.signal });
            // Guard against aborted request
            if (controller.signal.aborted) return;
            setTasks(sortTasksDesc(fetched || []));
        } catch (err: unknown) {
            if (controller.signal.aborted) return;
            if (err instanceof Error && err.name === 'AbortError') return;
            const msg = err instanceof Error ? err.message : 'Failed to fetch tasks';
            setError(msg);
        } finally {
            if (!controller.signal.aborted) {
                setLoading(false);
            }
        }
    }, []);

    // ── Create task (optimistic) ──

    const createTask = useCallback(async (data: Partial<TaskCard>): Promise<TaskCard | null> => {
        // Build an optimistic placeholder
        const tempId = `temp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const now = Date.now();
        const optimistic: TaskCard = {
            id: tempId,
            title: data.title || '',
            description: data.description || '',
            status: data.status || 'backlog',
            mode: data.mode || 'standard',
            owner: data.owner || '',
            progress: 0,
            priority: data.priority || 'medium',
            createdAt: now,
            updatedAt: now,
            runIds: [],
            relatedFiles: [],
            latestSummary: '',
            latestRisk: '',
            reviewVerdict: '',
            reviewIssues: [],
            selfcheckItems: [],
        };

        // Optimistic insert (prepend)
        setTasks((prev) => sortTasksDesc([optimistic, ...prev]));
        setError(null);

        try {
            const { task } = await apiCreateTask(data);
            // Replace optimistic placeholder with real task
            setTasks((prev) => sortTasksDesc(prev.map((t) => (t.id === tempId ? task : t))));
            return task;
        } catch (err: unknown) {
            // Rollback
            setTasks((prev) => prev.filter((t) => t.id !== tempId));
            const msg = err instanceof Error ? err.message : 'Failed to create task';
            setError(msg);
            return null;
        }
    }, []);

    // ── Update task (optimistic) ──

    const updateTask = useCallback(async (id: string, data: Partial<TaskCard>): Promise<TaskCard | null> => {
        // Snapshot for rollback
        let snapshot: TaskCard | undefined;
        const optimisticUpdatedAt = Date.now();

        setTasks((prev) =>
            sortTasksDesc(prev.map((t) => {
                if (t.id !== id) return t;
                snapshot = t;
                return { ...t, ...data, updatedAt: optimisticUpdatedAt };
            })),
        );
        setError(null);

        try {
            const { task } = await updateTaskApi(id, data);
            // Replace with canonical server response
            setTasks((prev) => sortTasksDesc(prev.map((t) => (t.id === id ? task : t))));
            return task;
        } catch (err: unknown) {
            // Roll back only if no newer canonical state has been merged since the optimistic write.
            if (snapshot) {
                setTasks((prev) => sortTasksDesc(prev.map((t) => {
                    if (t.id !== id) return t;
                    return canRollbackTask(t, snapshot!, optimisticUpdatedAt) ? snapshot! : t;
                })));
            }
            const msg = err instanceof Error ? err.message : 'Failed to update task';
            setError(msg);
            return null;
        }
    }, []);

    // ── Transition task (optimistic) ──

    const transitionTask = useCallback(async (id: string, status: TaskStatus): Promise<TaskCard | null> => {
        let snapshot: TaskCard | undefined;
        const optimisticUpdatedAt = Date.now();

        setTasks((prev) =>
            sortTasksDesc(prev.map((t) => {
                if (t.id !== id) return t;
                snapshot = t;
                return { ...t, status, updatedAt: optimisticUpdatedAt };
            })),
        );
        setError(null);

        try {
            const result = await apiTransitionTask(id, status);
            if (result.success && result.task) {
                setTasks((prev) => sortTasksDesc(prev.map((t) => (t.id === id ? result.task! : t))));
                return result.task;
            }
            // Server rejected — rollback
            if (snapshot) {
                setTasks((prev) => sortTasksDesc(prev.map((t) => {
                    if (t.id !== id) return t;
                    return canRollbackTask(t, snapshot!, optimisticUpdatedAt) ? snapshot! : t;
                })));
            }
            setError(result.error || 'Transition rejected');
            return null;
        } catch (err: unknown) {
            if (snapshot) {
                setTasks((prev) => sortTasksDesc(prev.map((t) => {
                    if (t.id !== id) return t;
                    return canRollbackTask(t, snapshot!, optimisticUpdatedAt) ? snapshot! : t;
                })));
            }
            const msg = err instanceof Error ? err.message : 'Failed to transition task';
            setError(msg);
            return null;
        }
    }, []);

    // ── Refresh single task ──

    const refreshTask = useCallback(async (id: string) => {
        refreshAbortRef.current?.abort();
        const controller = new AbortController();
        refreshAbortRef.current = controller;
        try {
            const { task } = await getTaskById(id, { signal: controller.signal });
            if (controller.signal.aborted) return;
            setTasks((prev) => {
                const exists = prev.some((t) => t.id === id);
                if (exists) {
                    return sortTasksDesc(prev.map((t) => (t.id === id ? task : t)));
                }
                // Task is new — prepend
                return sortTasksDesc([task, ...prev]);
            });
        } catch {
            // Silently ignore — task may have been deleted
        }
    }, []);

    // ── Select task ──

    const selectTask = useCallback((id: string | null) => {
        setSelectedTaskId(id);
    }, []);

    // ── Merge task from external source (e.g. WS broadcast) ──

    const mergeTask = useCallback((task: TaskMergePatch) => {
        setTasks((prev) => {
            const idx = prev.findIndex((t) => t.id === task.id);
            if (idx >= 0) {
                const current = prev[idx];
                const incomingUpdatedAt = task.updatedAt ?? current.updatedAt;
                if (isTaskPatchNewerOrEqual(task, current)) {
                    const next = [...prev];
                    next[idx] = {
                        ...current,
                        ...task,
                        updatedAt: incomingUpdatedAt,
                        version: hasComparableVersion(task.version)
                            ? task.version
                            : current.version,
                    };
                    return sortTasksDesc(next);
                }
                return prev;
            }
            return sortTasksDesc([buildTaskFromPatch(task), ...prev]);
        });
    }, []);

    return {
        tasks,
        selectedTask,
        selectTask,
        loading,
        error,
        fetchTasks,
        createTask,
        updateTask,
        transitionTask,
        refreshTask,
        tasksByStatus,
        mergeTask,
    };
}
