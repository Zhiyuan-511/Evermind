'use client';

import {
    createContext,
    useContext,
    useEffect,
    type ReactNode,
} from 'react';
import { useTaskManager, type UseTaskManagerReturn } from '@/hooks/useTaskManager';
import { useRunManager, type UseRunManagerReturn } from '@/hooks/useRunManager';

// ────────────────────────────────────────────────────────────────────
//  Two separate contexts so that Task‐list changes don't re‐render
//  RunTimeline/Inspector, and vice‑versa.
// ────────────────────────────────────────────────────────────────────

const TaskContext = createContext<UseTaskManagerReturn | null>(null);
const RunContext = createContext<UseRunManagerReturn | null>(null);

/**
 * Read‑only access to the page‑level task canonical state.
 *
 * Must be rendered inside `<TaskRunProvider>`.
 */
export function useTaskContext(): UseTaskManagerReturn {
    const ctx = useContext(TaskContext);
    if (!ctx) {
        throw new Error('useTaskContext must be used within <TaskRunProvider>');
    }
    return ctx;
}

/**
 * Read‑only access to the page‑level run canonical state.
 *
 * Must be rendered inside `<TaskRunProvider>`.
 */
export function useRunContext(): UseRunManagerReturn {
    const ctx = useContext(RunContext);
    if (!ctx) {
        throw new Error('useRunContext must be used within <TaskRunProvider>');
    }
    return ctx;
}

// ────────────────────────────────────────────────────────────────────
//  Provider
// ────────────────────────────────────────────────────────────────────

interface TaskRunProviderProps {
    children: ReactNode;
    /**
     * If true, auto‑poll active runs for real‑time status catch‑up.
     * Default: true.
     */
    autoPoll?: boolean;
    /**
     * Polling interval in ms (only used when autoPoll is true).
     * Default: 8000.
     */
    pollIntervalMs?: number;
}

/**
 * Single‑instance provider that owns the canonical Task + Run state
 * for the entire editor page.
 *
 * It instantiates:
 *   - `useTaskManager()` — task CRUD, list, selection
 *   - `useRunManager({ taskId })` — runs scoped to the selected task
 *
 * Downstream components access state via `useTaskContext()` and
 * `useRunContext()` without needing to instantiate their own hooks.
 */
export function TaskRunProvider({
    children,
    autoPoll = true,
    pollIntervalMs = 3000,
}: TaskRunProviderProps) {
    const taskManager = useTaskManager();

    const runManager = useRunManager({
        taskId: taskManager.selectedTask?.id ?? null,
        autoPoll,
        pollIntervalMs,
    });

    // Auto‑fetch the full task list once on mount.
    useEffect(() => {
        void taskManager.fetchTasks();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    return (
        <TaskContext.Provider value={taskManager}>
            <RunContext.Provider value={runManager}>
                {children}
            </RunContext.Provider>
        </TaskContext.Provider>
    );
}
