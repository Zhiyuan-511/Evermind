'use client';

import { useState, useEffect, useCallback } from 'react';
import { type TaskCard, type TaskStatus, type TaskPriority, type TaskMode, BOARD_COLUMNS, type RunReportRecord, type RunRecord, type RunStatus, type NodeExecutionRecord } from '@/lib/types';
import { getBoardSummary } from '@/lib/api';
import { useTaskContext, useRunContext } from '@/contexts/TaskRunProvider';
import TaskDetailPanel from './TaskDetailPanel';

interface TaskBoardModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
    sessionId?: string;
    runReports?: RunReportRecord[];
}

const PRIORITY_COLORS: Record<TaskPriority, string> = {
    urgent: '#ef4444',
    high: '#f59e0b',
    medium: '#3b82f6',
    low: '#64748b',
};

const SUB_STATUS_LABELS: Record<string, Record<string, string>> = {
    en: { review: '🔍 Under Review', selfcheck: '🧪 Self-Checking', executing: '⚡ Executing' },
    zh: { review: '🔍 审核中', selfcheck: '🧪 自检中', executing: '⚡ 执行中' },
};



const RUN_STATUS_META: Record<RunStatus, { color: string; label_en: string; label_zh: string }> = {
    queued:             { color: '#64748b', label_en: 'Queued', label_zh: '排队中' },
    running:            { color: '#3b82f6', label_en: 'Running', label_zh: '运行中' },
    waiting_review:     { color: '#f59e0b', label_en: 'Review', label_zh: '待审核' },
    waiting_selfcheck:  { color: '#06b6d4', label_en: 'Self-check', label_zh: '待自检' },
    failed:             { color: '#ef4444', label_en: 'Failed', label_zh: '失败' },
    done:               { color: '#22c55e', label_en: 'Done', label_zh: '已完成' },
    cancelled:          { color: '#6b7280', label_en: 'Cancelled', label_zh: '已取消' },
};

const ACTIVE_RUN_STATUSES: RunStatus[] = ['running', 'waiting_review', 'waiting_selfcheck'];

function timeAgo(timestamp: number, lang: string): string {
    const diff = Date.now() - timestamp;
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return lang === 'zh' ? '刚刚' : 'just now';
    if (mins < 60) return lang === 'zh' ? `${mins}分钟前` : `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return lang === 'zh' ? `${hours}小时前` : `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return lang === 'zh' ? `${days}天前` : `${days}d ago`;
}

function normalizeUiTimestamp(timestamp: number): number {
    return timestamp < 10_000_000_000 ? timestamp * 1000 : timestamp;
}

export default function TaskBoardModal({ open, onClose, lang, sessionId, runReports }: TaskBoardModalProps) {
    const [latestRuns, setLatestRuns] = useState<Record<string, RunRecord>>({});
    const [activeNodeLabelsByTask, setActiveNodeLabelsByTask] = useState<Record<string, string[]>>({});
    const {
        tasks,
        selectedTask,
        selectTask,
        loading,
        fetchTasks,
        createTask,
        updateTask,
        transitionTask,
        tasksByStatus,
        refreshTask,
    } = useTaskContext();
    const { runs, nodeExecutions } = useRunContext();

    // Remove create form — tasks are created automatically via run_goal

    const refreshBoardSummary = useCallback(async () => {
        try {
            const { tasks: summaryTasks } = await getBoardSummary({ sessionId: sessionId || undefined });
            const nextRuns: Record<string, RunRecord> = {};
            const nextLabels: Record<string, string[]> = {};
            for (const task of summaryTasks) {
                if (task.latestRun) {
                    nextRuns[task.id] = task.latestRun;
                }
                const labels = Array.isArray(task.activeNodeLabels) && task.activeNodeLabels.length > 0
                    ? task.activeNodeLabels
                    : (task.activeNodeLabel ? [task.activeNodeLabel] : []);
                if (labels.length > 0) {
                    nextLabels[task.id] = labels.slice(0, 3);
                }
            }
            setLatestRuns(nextRuns);
            setActiveNodeLabelsByTask(nextLabels);
        } catch {
            // Ignore transient board summary failures; canonical task state still renders.
        }
    }, [sessionId]);

    // P1-2: Inject pulse keyframes once
    useEffect(() => {
        if (typeof window === 'undefined') return;
        const STYLE_ID = 'evermind-task-board-pulse';
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = `
            @keyframes executingPulse {
                0%, 100% { box-shadow: 0 0 12px rgba(79, 143, 255, 0.15); }
                50% { box-shadow: 0 0 20px rgba(79, 143, 255, 0.25); }
            }
        `;
        document.head.appendChild(style);
    }, []);

    // P1-2: Helper to get active node labels for a task
    const getActiveNodeLabels = useCallback((taskId: string): string[] => {
        const selectedTaskLabels = runs
            .filter((run) => run.task_id === taskId && run.status === 'running')
            .flatMap((run) => (run.active_node_execution_ids || [])
                .map((id) => nodeExecutions.find((ne) => ne.id === id))
                .filter((ne): ne is NodeExecutionRecord => Boolean(ne))
                .map((ne) => ne.node_label || ne.node_key))
            .filter(Boolean)
            .slice(0, 2);
        if (selectedTaskLabels.length > 0) {
            return selectedTaskLabels;
        }
        return activeNodeLabelsByTask[taskId] || [];
    }, [activeNodeLabelsByTask, nodeExecutions, runs]);

    useEffect(() => {
        if (open) {
            void fetchTasks();
            void refreshBoardSummary();
        }
    }, [open, fetchTasks, refreshBoardSummary]);

    useEffect(() => {
        if (!open) {
            selectTask(null);
            setLatestRuns({});
            setActiveNodeLabelsByTask({});
        }
    }, [open, selectTask]);

    // B-4: Auto-select the latest executing task when board opens
    useEffect(() => {
        if (!open || selectedTask || tasks.length === 0) return;
        const executing = tasks.filter(t => t.status === 'executing');
        if (executing.length > 0) {
            // Pick the most recently updated executing task
            const latest = executing.reduce((a, b) =>
                (b.updatedAt || 0) > (a.updatedAt || 0) ? b : a
            );
            selectTask(latest.id);
        }
    }, [open, selectedTask, tasks, selectTask]);

    useEffect(() => {
        if (!open) return;
        if (tasks.length === 0) {
            setLatestRuns({});
            setActiveNodeLabelsByTask({});
            return;
        }
        void refreshBoardSummary();
    }, [open, refreshBoardSummary, tasks.length]);

    useEffect(() => {
        if (!open) return;
        const hasActiveTask = tasks.some((task) => {
            const latestRun = latestRuns[task.id];
            return task.status === 'executing' || Boolean(latestRun && ACTIVE_RUN_STATUSES.includes(latestRun.status));
        });
        if (!hasActiveTask && tasks.length > 0) {
            return;
        }

        void refreshBoardSummary();
        const timer = window.setInterval(() => {
            void refreshBoardSummary();
        }, 3000);
        return () => {
            window.clearInterval(timer);
        };
    }, [latestRuns, open, refreshBoardSummary, tasks]);

    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);

    if (!open) return null;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div
                className="modal-container"
                style={{ width: '95vw', maxWidth: 1400, maxHeight: '90vh' }}
                onClick={(e) => e.stopPropagation()}
            >
                {/* Header */}
                <div className="modal-header" style={{ borderBottom: '1px solid var(--glass-border)' }}>
                    <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        {tr('任务看板', 'Task Board')}
                        <span style={{ fontSize: 11, color: 'var(--text3)', fontWeight: 400, marginLeft: 8 }}>
                            {tasks.length} {tr('个任务', 'tasks')}
                        </span>
                    </h3>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <button
                            className="btn"
                            onClick={() => {
                                void fetchTasks();
                                void refreshBoardSummary();
                            }}
                            style={{ fontSize: 11, padding: '5px 10px' }}
                            title={tr('刷新', 'Refresh')}
                        >
                            🔄 {tr('刷新', 'Refresh')}
                        </button>
                        <button className="modal-close" onClick={onClose}>✕</button>
                    </div>
                </div>

                {/* Board — 3 columns */}
                <div style={{
                    flex: 1,
                    overflow: 'auto',
                    padding: 12,
                    display: 'flex',
                    gap: 12,
                    minHeight: 0,
                }}>
                    {(() => {
                        // Filter tasks by session
                        const filteredTasks = sessionId
                            ? tasks.filter(t => t.sessionId === sessionId)
                            : tasks;
                        if (filteredTasks.length === 0) return (
                        /* Empty state guidance */
                        <div style={{
                            flex: 1, display: 'flex', flexDirection: 'column',
                            alignItems: 'center', justifyContent: 'center',
                            gap: 12, padding: '40px 20px',
                        }}>
                            <div style={{ fontSize: 40, opacity: 0.6 }}>📋</div>
                            <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text1)' }}>
                                {tr('还没有任务记录', 'No tasks yet')}
                            </div>
                            <div style={{
                                fontSize: 12, color: 'var(--text3)', textAlign: 'center',
                                maxWidth: 400, lineHeight: 1.8,
                            }}>
                                {tr(
                                    '看板会自动记录每次任务的执行过程和结果。\n\n' +
                                    '💬 在聊天窗口发送任务目标（如「做一个卖衣服的网站」）\n' +
                                    '☁️ 或通过 OpenClaw 发送任务\n\n' +
                                    '任务开始后，看板会实时显示：\n' +
                                    '• 哪些节点正在执行\n' +
                                    '• 当前进度和状态\n' +
                                    '• 完成后的结果和报告',
                                    'The board automatically records each task\'s execution and results.\n\n' +
                                    '💬 Send a goal in the chat (e.g. "Build a clothing website")\n' +
                                    '☁️ Or send a task via OpenClaw\n\n' +
                                    'Once a task starts, the board shows:\n' +
                                    '• Which nodes are executing\n' +
                                    '• Current progress and status\n' +
                                    '• Results and reports when done'
                                )}
                            </div>
                        </div>
                        );
                        return BOARD_COLUMNS.map((col) => {
                            const colTasks = filteredTasks.filter(t => col.statuses.includes(t.status))
                                .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
                            return (
                                <div
                                    key={col.key}
                                    style={{
                                        flex: '1 1 0',
                                        minWidth: 240,
                                        display: 'flex',
                                        flexDirection: 'column',
                                        gap: 8,
                                        background: 'var(--glass)',
                                        borderRadius: 12,
                                        border: '1px solid var(--glass-border)',
                                        padding: '8px 8px',
                                    }}
                                >
                                    {/* Column header */}
                                    <div style={{
                                        display: 'flex', alignItems: 'center', gap: 6,
                                        padding: '6px 10px', borderRadius: 8,
                                        background: `${col.color}12`,
                                        fontWeight: 700,
                                    }}>
                                        <span style={{ fontSize: 13, color: col.color }}>
                                            {lang === 'zh' ? col.label_zh : col.label_en}
                                        </span>
                                        <span style={{
                                            fontSize: 11, color: 'var(--text3)',
                                            background: 'var(--glass)', borderRadius: 10,
                                            padding: '1px 8px', marginLeft: 'auto',
                                        }}>
                                            {colTasks.length}
                                        </span>
                                    </div>

                                    {/* Task cards */}
                                    <div style={{
                                        flex: 1, overflow: 'auto', display: 'flex',
                                        flexDirection: 'column', gap: 6, minHeight: 60,
                                    }}>
                                        {colTasks.map((task) => {
                                            const isSelected = selectedTask?.id === task.id;
                                            const isExecuting = ['executing', 'review', 'selfcheck'].includes(task.status);
                                            const nodeLabels = isExecuting ? getActiveNodeLabels(task.id) : [];
                                            const lr = latestRuns[task.id];
                                            const lrMeta = lr ? (RUN_STATUS_META[lr.status] || RUN_STATUS_META.queued) : null;
                                            return (
                                                <div
                                                    key={task.id}
                                                    onClick={() => selectTask(task.id)}
                                                    style={{
                                                        padding: '10px 12px',
                                                        borderRadius: 10,
                                                        background: isExecuting ? 'rgba(79, 143, 255, 0.04)' : 'var(--surface)',
                                                        border: isSelected
                                                            ? '1.5px solid rgba(168, 85, 247, 0.5)'
                                                            : isExecuting
                                                                ? '1px solid rgba(79, 143, 255, 0.35)'
                                                                : '1px solid var(--glass-border)',
                                                        cursor: 'pointer',
                                                        transition: 'all 0.15s',
                                                        ...(isExecuting ? {
                                                            animation: 'executingPulse 2s ease-in-out infinite',
                                                        } : {}),
                                                    }}
                                                >
                                                    {/* Title */}
                                                    <div style={{
                                                        fontSize: 12, fontWeight: 600, color: 'var(--text1)',
                                                        overflow: 'hidden', textOverflow: 'ellipsis',
                                                        whiteSpace: 'nowrap', marginBottom: 4,
                                                    }}>
                                                        {task.title}
                                                    </div>

                                                    {/* Sub-status badge for active column (review/selfcheck) */}
                                                    {isExecuting && task.status !== 'executing' && (
                                                        <div style={{
                                                            fontSize: 9, fontWeight: 600,
                                                            color: task.status === 'review' ? '#f59e0b' : '#06b6d4',
                                                            marginBottom: 4,
                                                        }}>
                                                            {SUB_STATUS_LABELS[lang]?.[task.status] || task.status}
                                                        </div>
                                                    )}

                                                    {/* Active node labels */}
                                                    {nodeLabels.length > 0 && (
                                                        <div style={{
                                                            display: 'flex', alignItems: 'center',
                                                            gap: 4, marginBottom: 4,
                                                            fontSize: 10, color: '#3b82f6',
                                                        }}>
                                                            <span style={{
                                                                width: 5, height: 5, borderRadius: '50%',
                                                                background: '#3b82f6',
                                                                animation: 'executingPulse 1.5s ease-in-out infinite',
                                                                flexShrink: 0,
                                                            }} />
                                                            <span style={{
                                                                overflow: 'hidden', textOverflow: 'ellipsis',
                                                                whiteSpace: 'nowrap',
                                                            }}>
                                                                {nodeLabels.join(' → ')}
                                                            </span>
                                                        </div>
                                                    )}

                                                    {/* Progress bar */}
                                                    {task.progress > 0 && task.progress < 100 && (
                                                        <div className="progress-bar" style={{ marginBottom: 4, height: 3 }}>
                                                            <div className="fill" style={{ width: `${task.progress}%` }} />
                                                        </div>
                                                    )}

                                                    {/* Latest run status */}
                                                    {lr && lrMeta && (
                                                        <div style={{
                                                            padding: '3px 6px', borderRadius: 5,
                                                            background: `${lrMeta.color}08`,
                                                            border: `1px solid ${lrMeta.color}18`,
                                                            display: 'flex', alignItems: 'center', gap: 4,
                                                            marginBottom: 4,
                                                        }}>
                                                            <span style={{
                                                                width: 6, height: 6, borderRadius: '50%',
                                                                background: lrMeta.color, flexShrink: 0,
                                                            }} />
                                                            <span style={{
                                                                fontSize: 9, fontWeight: 600, color: lrMeta.color,
                                                                flex: 1, overflow: 'hidden',
                                                                textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                                            }}>
                                                                {lr.summary
                                                                    ? (lr.summary.length > 40 ? lr.summary.slice(0, 40) + '…' : lr.summary)
                                                                    : (lang === 'zh' ? lrMeta.label_zh : lrMeta.label_en)}
                                                            </span>
                                                        </div>
                                                    )}

                                                    {/* Footer: time + mode */}
                                                    <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 9, color: 'var(--text3)' }}>
                                                        <span>{timeAgo(normalizeUiTimestamp(task.updatedAt), lang)}</span>
                                                        {task.mode === 'pro' && (
                                                            <span style={{
                                                                padding: '0px 4px', borderRadius: 3,
                                                                background: 'rgba(168, 85, 247, 0.12)',
                                                                color: '#a855f7', fontSize: 8, fontWeight: 600,
                                                                marginLeft: 'auto',
                                                            }}>Pro</span>
                                                        )}
                                                    </div>
                                                </div>
                                            );
                                        })}

                                        {colTasks.length === 0 && (
                                            <div style={{
                                                flex: 1, display: 'flex', alignItems: 'center',
                                                justifyContent: 'center', color: 'var(--text3)',
                                                fontSize: 11, opacity: 0.5, minHeight: 60,
                                            }}>
                                                {col.key === 'pending' ? tr('暂无待执行任务', 'No pending tasks')
                                                    : col.key === 'active' ? tr('暂无执行中任务', 'No active tasks')
                                                    : tr('暂无已完成任务', 'No completed tasks')}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            );
                        });
                    })()}
                </div>

                {/* Task Detail Panel */}
                {selectedTask && (
                    <TaskDetailPanel
                        key={selectedTask.id}
                        task={selectedTask}
                        lang={lang}
                        onClose={() => selectTask(null)}
                        onBoardClose={onClose}
                        onTransition={(newStatus) => { void transitionTask(selectedTask.id, newStatus); }}
                        onUpdate={(data) => { void updateTask(selectedTask.id, data); }}
                        onRunActivity={async () => {
                            await refreshBoardSummary();
                            await refreshTask(selectedTask.id);
                        }}
                        runReports={runReports?.filter((report) =>
                            report.taskId === selectedTask.id
                            || Boolean(report.runId && selectedTask.runIds?.includes(report.runId))
                            || selectedTask.runIds?.includes(report.id)
                        ) || []}
                    />
                )}
            </div>
        </div>
    );
}
