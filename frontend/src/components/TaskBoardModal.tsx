'use client';

import { useState, useEffect, useCallback } from 'react';
import { type TaskCard, type TaskStatus, type TaskPriority, type TaskMode, TASK_COLUMNS, type RunReportRecord, type RunRecord, type RunStatus } from '@/lib/types';
import { listRuns } from '@/lib/api';
import { useTaskContext } from '@/contexts/TaskRunProvider';
import TaskDetailPanel from './TaskDetailPanel';

interface TaskBoardModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
    runReports?: RunReportRecord[];
}

const PRIORITY_COLORS: Record<TaskPriority, string> = {
    urgent: '#ef4444',
    high: '#f59e0b',
    medium: '#3b82f6',
    low: '#64748b',
};

const PRIORITY_LABELS: Record<string, Record<TaskPriority, string>> = {
    en: { urgent: 'Urgent', high: 'High', medium: 'Medium', low: 'Low' },
    zh: { urgent: '紧急', high: '高', medium: '中', low: '低' },
};

const MODE_LABELS: Record<string, Record<TaskMode, string>> = {
    en: { standard: 'Standard', pro: 'Pro', debug: 'Debug', review: 'Review' },
    zh: { standard: '标准', pro: '专业', debug: '调试', review: '审核' },
};

const RUN_STATUS_ICONS: Record<RunStatus, { icon: string; color: string }> = {
    queued:             { icon: '⏳', color: '#64748b' },
    running:            { icon: '⚡', color: '#3b82f6' },
    waiting_review:     { icon: '👁', color: '#f59e0b' },
    waiting_selfcheck:  { icon: '🧪', color: '#06b6d4' },
    failed:             { icon: '❌', color: '#ef4444' },
    done:               { icon: '✅', color: '#22c55e' },
    cancelled:          { icon: '🚫', color: '#6b7280' },
};

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

export default function TaskBoardModal({ open, onClose, lang, runReports }: TaskBoardModalProps) {
    const [showCreate, setShowCreate] = useState(false);
    const [dragId, setDragId] = useState<string | null>(null);
    const [latestRuns, setLatestRuns] = useState<Record<string, RunRecord>>({});
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

    // Create form state
    const [newTitle, setNewTitle] = useState('');
    const [newDesc, setNewDesc] = useState('');
    const [newMode, setNewMode] = useState<TaskMode>('standard');
    const [newPriority, setNewPriority] = useState<TaskPriority>('medium');

    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);

    const refreshLatestRunForTask = useCallback(async (taskId: string) => {
        try {
            const { runs } = await listRuns(taskId);
            setLatestRuns((prev) => {
                const next = { ...prev };
                if (runs?.[0]) next[taskId] = runs[0];
                else delete next[taskId];
                return next;
            });
        } catch {
            // ignore refresh errors
        }
    }, []);

    useEffect(() => {
        if (open) {
            void fetchTasks();
        }
    }, [open, fetchTasks]);

    useEffect(() => {
        if (!open) {
            selectTask(null);
            setLatestRuns({});
        }
    }, [open, selectTask]);

    // Fetch latest run for each task that has run_ids
    useEffect(() => {
        const tasksWithRuns = tasks.filter((task) => task.runIds?.length > 0);
        if (tasksWithRuns.length === 0) {
            setLatestRuns({});
            return;
        }
        let cancelled = false;
        (async () => {
            const resolved = await Promise.all(tasksWithRuns.map(async (task) => {
                try {
                    const { runs } = await listRuns(task.id);
                    return [task.id, runs?.[0] || null] as const;
                } catch {
                    return [task.id, null] as const;
                }
            }));
            if (cancelled) return;
            const nextLatestRuns: Record<string, RunRecord> = {};
            resolved.forEach(([taskId, run]) => {
                if (run) nextLatestRuns[taskId] = run;
            });
            setLatestRuns(nextLatestRuns);
        })();
        return () => {
            cancelled = true;
        };
    }, [tasks]);

    const handleCreate = async () => {
        if (!newTitle.trim()) return;
        const created = await createTask({
            title: newTitle.trim(),
            description: newDesc.trim(),
            mode: newMode,
            priority: newPriority,
        });
        if (created) {
            setNewTitle('');
            setNewDesc('');
            setNewMode('standard');
            setNewPriority('medium');
            setShowCreate(false);
        }
    };

    const handleTransition = async (taskId: string, newStatus: TaskStatus) => {
        await transitionTask(taskId, newStatus);
    };

    const handleUpdateTask = async (taskId: string, data: Partial<TaskCard>) => {
        await updateTask(taskId, data);
    };

    const handleDragStart = (taskId: string) => {
        setDragId(taskId);
    };

    const handleDrop = (targetStatus: TaskStatus) => {
        if (!dragId) return;
        const task = tasks.find((t) => t.id === dragId);
        if (task && task.status !== targetStatus) {
            handleTransition(dragId, targetStatus);
        }
        setDragId(null);
    };

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
                        <span style={{ fontSize: 18 }}>📋</span>
                        {tr('任务看板', 'Task Board')}
                        <span style={{ fontSize: 11, color: 'var(--text3)', fontWeight: 400, marginLeft: 8 }}>
                            {tasks.length} {tr('个任务', 'tasks')}
                        </span>
                    </h3>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <button
                            className="btn btn-primary"
                            onClick={() => setShowCreate(true)}
                            style={{ fontSize: 11, padding: '5px 12px' }}
                        >
                            + {tr('新建任务', 'New Task')}
                        </button>
                        <button
                            className="btn"
                            onClick={() => void fetchTasks()}
                            style={{ fontSize: 11, padding: '5px 10px' }}
                            title={tr('刷新', 'Refresh')}
                        >
                            🔄
                        </button>
                        <button className="modal-close" onClick={onClose}>✕</button>
                    </div>
                </div>

                {/* Create Task Inline Form */}
                {showCreate && (
                    <div style={{
                        padding: '12px 16px',
                        borderBottom: '1px solid var(--glass-border)',
                        background: 'rgba(79, 143, 255, 0.04)',
                        animation: 'fadeIn 0.15s ease-out',
                    }}>
                        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                            <input
                                className="s-input"
                                placeholder={tr('任务标题...', 'Task title...')}
                                value={newTitle}
                                onChange={(e) => setNewTitle(e.target.value)}
                                onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
                                style={{ flex: '1 1 200px', minWidth: 200 }}
                                autoFocus
                            />
                            <input
                                className="s-input"
                                placeholder={tr('描述（可选）', 'Description (optional)')}
                                value={newDesc}
                                onChange={(e) => setNewDesc(e.target.value)}
                                style={{ flex: '2 1 300px', minWidth: 200 }}
                            />
                            <select
                                className="s-input"
                                value={newMode}
                                onChange={(e) => setNewMode(e.target.value as TaskMode)}
                                style={{ flex: '0 0 100px' }}
                            >
                                {(['standard', 'pro', 'debug', 'review'] as TaskMode[]).map((m) => (
                                    <option key={m} value={m}>{MODE_LABELS[lang][m]}</option>
                                ))}
                            </select>
                            <select
                                className="s-input"
                                value={newPriority}
                                onChange={(e) => setNewPriority(e.target.value as TaskPriority)}
                                style={{ flex: '0 0 80px' }}
                            >
                                {(['low', 'medium', 'high', 'urgent'] as TaskPriority[]).map((p) => (
                                    <option key={p} value={p}>{PRIORITY_LABELS[lang][p]}</option>
                                ))}
                            </select>
                            <button className="btn btn-success" onClick={handleCreate} style={{ fontSize: 11 }}>
                                {tr('创建', 'Create')}
                            </button>
                            <button className="btn" onClick={() => setShowCreate(false)} style={{ fontSize: 11 }}>
                                {tr('取消', 'Cancel')}
                            </button>
                        </div>
                    </div>
                )}

                {/* Board */}
                <div style={{
                    flex: 1,
                    overflow: 'auto',
                    padding: 12,
                    display: 'flex',
                    gap: 10,
                    minHeight: 0,
                }}>
                    {loading && tasks.length === 0 ? (
                        <div style={{
                            flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
                            color: 'var(--text3)', fontSize: 13,
                        }}>
                            {tr('加载中...', 'Loading...')}
                        </div>
                    ) : (
                        TASK_COLUMNS.map((col) => {
                            const colTasks = tasksByStatus(col.key);
                            return (
                                <div
                                    key={col.key}
                                    className="task-column"
                                    onDragOver={(e) => { e.preventDefault(); e.currentTarget.classList.add('drag-over'); }}
                                    onDragLeave={(e) => e.currentTarget.classList.remove('drag-over')}
                                    onDrop={(e) => { e.preventDefault(); e.currentTarget.classList.remove('drag-over'); handleDrop(col.key); }}
                                    style={{
                                        flex: '1 1 0',
                                        minWidth: 180,
                                        maxWidth: 260,
                                        display: 'flex',
                                        flexDirection: 'column',
                                        gap: 8,
                                        background: 'var(--glass)',
                                        borderRadius: 12,
                                        border: '1px solid var(--glass-border)',
                                        padding: '8px 6px',
                                    }}
                                >
                                    {/* Column header */}
                                    <div style={{
                                        display: 'flex', alignItems: 'center', gap: 6,
                                        padding: '4px 8px', borderRadius: 8,
                                        background: `${col.color}12`,
                                    }}>
                                        <span>{col.icon}</span>
                                        <span style={{ fontSize: 11, fontWeight: 700, color: col.color }}>
                                            {lang === 'zh' ? col.label_zh : col.label_en}
                                        </span>
                                        <span style={{
                                            fontSize: 10, color: 'var(--text3)',
                                            background: 'var(--glass)', borderRadius: 10,
                                            padding: '1px 6px', marginLeft: 'auto',
                                        }}>
                                            {colTasks.length}
                                        </span>
                                    </div>

                                    {/* Task cards */}
                                    <div style={{
                                        flex: 1, overflow: 'auto', display: 'flex',
                                        flexDirection: 'column', gap: 6, minHeight: 60,
                                    }}>
                                        {colTasks.map((task) => (
                                            <div
                                                key={task.id}
                                                draggable
                                                onDragStart={() => handleDragStart(task.id)}
                                                onClick={() => selectTask(task.id)}
                                                className="task-card-item"
                                                style={{
                                                    padding: '8px 10px',
                                                    borderRadius: 8,
                                                    background: 'var(--surface)',
                                                    border: '1px solid var(--glass-border)',
                                                    cursor: 'pointer',
                                                    transition: 'all 0.15s',
                                                    opacity: dragId === task.id ? 0.5 : 1,
                                                }}
                                            >
                                                {/* Priority dot + Title */}
                                                <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                                                    <span style={{
                                                        width: 6, height: 6, borderRadius: '50%',
                                                        background: PRIORITY_COLORS[task.priority] || '#64748b',
                                                        flexShrink: 0,
                                                    }} />
                                                    <span style={{
                                                        fontSize: 11, fontWeight: 600, color: 'var(--text1)',
                                                        overflow: 'hidden', textOverflow: 'ellipsis',
                                                        whiteSpace: 'nowrap', flex: 1,
                                                    }}>
                                                        {task.title}
                                                    </span>
                                                </div>

                                                {/* Meta row */}
                                                <div style={{
                                                    display: 'flex', alignItems: 'center', gap: 4,
                                                    fontSize: 9, color: 'var(--text3)', flexWrap: 'wrap',
                                                }}>
                                                    {/* Mode badge */}
                                                    <span style={{
                                                        padding: '1px 5px', borderRadius: 4,
                                                        background: task.mode === 'pro' ? 'rgba(168, 85, 247, 0.12)' : 'var(--glass)',
                                                        color: task.mode === 'pro' ? '#a855f7' : 'var(--text3)',
                                                        border: '1px solid ' + (task.mode === 'pro' ? 'rgba(168, 85, 247, 0.2)' : 'var(--glass-border)'),
                                                    }}>
                                                        {MODE_LABELS[lang][task.mode] || task.mode}
                                                    </span>
                                                    {/* Owner */}
                                                    {task.owner && (
                                                        <span style={{
                                                            padding: '1px 5px', borderRadius: 4,
                                                            background: 'var(--glass)',
                                                            border: '1px solid var(--glass-border)',
                                                        }}>
                                                            {task.owner}
                                                        </span>
                                                    )}
                                                    {/* Run count */}
                                                    {task.runIds?.length > 0 && (
                                                        <span style={{ marginLeft: 'auto' }}>
                                                            🔄 {task.runIds.length}
                                                        </span>
                                                    )}
                                                </div>

                                                {/* Progress bar */}
                                                {task.progress > 0 && task.progress < 100 && (
                                                    <div className="progress-bar" style={{ marginTop: 6, height: 2 }}>
                                                        <div className="fill" style={{ width: `${task.progress}%` }} />
                                                    </div>
                                                )}

                                                {/* Latest run status */}
                                                {latestRuns[task.id] && (() => {
                                                    const lr = latestRuns[task.id];
                                                    const rMeta = RUN_STATUS_ICONS[lr.status] || RUN_STATUS_ICONS.queued;
                                                    return (
                                                        <div style={{
                                                            marginTop: 5, padding: '3px 6px', borderRadius: 5,
                                                            background: `${rMeta.color}08`,
                                                            border: `1px solid ${rMeta.color}18`,
                                                            display: 'flex', alignItems: 'center', gap: 4,
                                                        }}>
                                                            <span style={{ fontSize: 9 }}>{rMeta.icon}</span>
                                                            <span style={{
                                                                fontSize: 8, fontWeight: 600, color: rMeta.color,
                                                                flex: 1, overflow: 'hidden',
                                                                textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                                            }}>
                                                                {lr.summary
                                                                    ? (lr.summary.length > 40 ? lr.summary.slice(0, 40) + '…' : lr.summary)
                                                                    : lr.status.replace(/_/g, ' ')}
                                                            </span>
                                                            {(lr.node_execution_ids?.length || 0) > 0 && (
                                                                <span style={{ fontSize: 7, color: 'var(--text3)' }}>
                                                                    {lr.node_execution_ids.length} nodes
                                                                </span>
                                                            )}
                                                        </div>
                                                    );
                                                })()}

                                                {/* Timestamp */}
                                                <div style={{ fontSize: 8, color: 'var(--text3)', marginTop: 4, textAlign: 'right' }}>
                                                    {timeAgo(normalizeUiTimestamp(task.updatedAt), lang)}
                                                </div>
                                            </div>
                                        ))}

                                        {colTasks.length === 0 && (
                                            <div style={{
                                                flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
                                                color: 'var(--text3)', fontSize: 10, opacity: 0.6, minHeight: 40,
                                            }}>
                                                {tr('拖拽任务到这里', 'Drop tasks here')}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            );
                        })
                    )}
                </div>

                {/* Task Detail Panel */}
                {selectedTask && (
                    <TaskDetailPanel
                        key={selectedTask.id}
                        task={selectedTask}
                        lang={lang}
                        onClose={() => selectTask(null)}
                        onTransition={(newStatus) => handleTransition(selectedTask.id, newStatus)}
                        onUpdate={(data) => handleUpdateTask(selectedTask.id, data)}
                        onRunActivity={async () => {
                            await refreshLatestRunForTask(selectedTask.id);
                            await refreshTask(selectedTask.id);
                        }}
                        runReports={runReports?.filter((r) => selectedTask.runIds?.includes(r.id)) || []}
                    />
                )}
            </div>
        </div>
    );
}
