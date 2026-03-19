'use client';

import { useState, useEffect, useCallback, useRef, type CSSProperties } from 'react';
import type { RunRecord, NodeExecutionRecord, RunStatus, NodeExecutionStatus } from '@/lib/types';
import { useRunContext } from '@/contexts/TaskRunProvider';
import { cancelRun as cancelRunApi } from '@/lib/api';

interface RunTimelineProps {
    taskId: string;
    lang: 'en' | 'zh';
    onNodeSelect?: (nodeExec: NodeExecutionRecord) => void;
    onRunSelect?: (run: RunRecord) => void;
    pollIntervalMs?: number;
    refreshKey?: number;
    onRunsChanged?: () => Promise<void> | void;
}

const RUN_STATUS_META: Record<RunStatus, { color: string; label_en: string; label_zh: string }> = {
    queued:             { color: '#64748b', label_en: 'Queued',             label_zh: '排队中' },
    running:            { color: '#3b82f6', label_en: 'Running',            label_zh: '运行中' },
    waiting_review:     { color: '#f59e0b', label_en: 'Waiting Review',     label_zh: '等待审核' },
    waiting_selfcheck:  { color: '#06b6d4', label_en: 'Waiting Self-Check', label_zh: '等待自检' },
    failed:             { color: '#ef4444', label_en: 'Failed',             label_zh: '失败' },
    done:               { color: '#22c55e', label_en: 'Done',               label_zh: '完成' },
    cancelled:          { color: '#6b7280', label_en: 'Cancelled',          label_zh: '已取消' },
};

const NODE_STATUS_META: Record<NodeExecutionStatus, { color: string; label_en: string; label_zh: string }> = {
    queued:            { color: '#64748b', label_en: 'Queued',            label_zh: '排队' },
    running:           { color: '#3b82f6', label_en: 'Running',           label_zh: '运行中' },
    passed:            { color: '#22c55e', label_en: 'Passed',            label_zh: '通过' },
    failed:            { color: '#ef4444', label_en: 'Failed',            label_zh: '失败' },
    blocked:           { color: '#f59e0b', label_en: 'Blocked',           label_zh: '阻塞' },
    waiting_approval:  { color: '#a855f7', label_en: 'Awaiting Approval', label_zh: '等待审批' },
    skipped:           { color: '#6b7280', label_en: 'Skipped',           label_zh: '已跳过' },
    cancelled:         { color: '#78716c', label_en: 'Cancelled',         label_zh: '已取消' },
};

function chipStyle(color = 'var(--text3)', background = 'rgba(255,255,255,0.03)'): CSSProperties {
    return {
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '1px 6px',
        borderRadius: 999,
        fontSize: 8,
        fontWeight: 600,
        color,
        background,
        border: `1px solid ${color === 'var(--text3)' ? 'var(--glass-border)' : `${color}25`}`,
    };
}

function dotStyle(color: string, glow = false): CSSProperties {
    return {
        width: 6,
        height: 6,
        borderRadius: '50%',
        background: color,
        flexShrink: 0,
        boxShadow: glow ? `0 0 8px ${color}80` : 'none',
    };
}

function formatTs(epochSec: number, lang: 'en' | 'zh'): string {
    if (!epochSec) return '--';
    const d = new Date(epochSec < 1e12 ? epochSec * 1000 : epochSec);
    return d.toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-US', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
    });
}

function durationStr(startSec: number, endSec: number): string {
    if (!startSec) return '';
    const end = endSec || Date.now() / 1000;
    const diff = Math.max(0, Math.round(end - startSec));
    if (diff < 60) return `${diff}s`;
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    return `${m}m ${s}s`;
}

function costStr(cost: number): string {
    if (!cost) return '';
    return `$${cost.toFixed(3)}`;
}

export default function RunTimeline({
    taskId,
    lang,
    onNodeSelect,
    onRunSelect,
    refreshKey = 0,
    onRunsChanged,
}: RunTimelineProps) {
    const [actionError, setActionError] = useState('');
    const [cancellingRunId, setCancellingRunId] = useState<string | null>(null);
    const refreshKeyRef = useRef(refreshKey);
    const autoExpandedTaskRef = useRef<string | null>(null);
    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);
    const {
        runs,
        selectedRun,
        latestRun,
        selectRun,
        nodeExecutions,
        loading,
        nodesLoading,
        fetchRuns,
        fetchNodeExecutions,
    } = useRunContext();
    const expandedRunId = selectedRun?.id || null;

    useEffect(() => {
        setActionError('');
        setCancellingRunId(null);
        autoExpandedTaskRef.current = null;
    }, [taskId]);

    useEffect(() => {
        setActionError('');
    }, [expandedRunId]);

    useEffect(() => {
        if (!selectedRun && latestRun && autoExpandedTaskRef.current !== taskId) {
            autoExpandedTaskRef.current = taskId;
            selectRun(latestRun.id);
        }
    }, [latestRun, selectedRun, selectRun, taskId]);

    useEffect(() => {
        if (refreshKeyRef.current === refreshKey) return;
        refreshKeyRef.current = refreshKey;
        void fetchRuns();
    }, [fetchRuns, refreshKey]);

    useEffect(() => {
        if (expandedRunId) {
            void fetchNodeExecutions(expandedRunId);
        }
    }, [expandedRunId, refreshKey, fetchNodeExecutions]);

    const toggleRun = (runId: string) => {
        selectRun(expandedRunId === runId ? null : runId);
    };

    const canCancelRun = (status: RunStatus) =>
        status === 'queued' || status === 'running' || status === 'waiting_review' || status === 'waiting_selfcheck';

    const handleCancelRun = useCallback(async (runId: string) => {
        try {
            setCancellingRunId(runId);
            setActionError('');
            const result = await cancelRunApi(runId);
            if (!result?.success) throw new Error('Failed to cancel run');
            await fetchRuns();
            await fetchNodeExecutions(runId);
            await onRunsChanged?.();
        } catch (e) {
            setActionError(e instanceof Error ? e.message : tr('取消运行失败，请稍后重试', 'Failed to cancel run. Please try again.'));
        } finally {
            setCancellingRunId(null);
        }
    }, [fetchNodeExecutions, fetchRuns, onRunsChanged, tr]);

    if (loading && runs.length === 0) {
        return (
            <div style={{ textAlign: 'center', color: 'var(--text3)', fontSize: 11, padding: 24 }}>
                {tr('加载中…', 'Loading…')}
            </div>
        );
    }

    if (runs.length === 0) {
        return (
            <div style={{ textAlign: 'center', color: 'var(--text3)', fontSize: 11, padding: 24 }}>
                {tr('暂无运行记录，启动第一次运行吧', 'No runs yet. Start your first run!')}
            </div>
        );
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {runs.map((run, runIdx) => {
                const meta = RUN_STATUS_META[run.status] || RUN_STATUS_META.queued;
                const isExpanded = expandedRunId === run.id;
                const nodes = isExpanded ? nodeExecutions : [];

                return (
                    <div key={run.id} style={{
                        borderRadius: 10,
                        border: `1px solid ${isExpanded ? meta.color + '40' : 'var(--glass-border)'}`,
                        background: isExpanded ? `${meta.color}06` : 'var(--glass)',
                        overflow: 'hidden',
                        transition: 'all 0.2s ease',
                    }}>
                        {/* Run Header */}
                        <div
                            onClick={() => { toggleRun(run.id); onRunSelect?.(run); }}
                            style={{
                                padding: '10px 14px',
                                cursor: 'pointer',
                                display: 'flex', alignItems: 'center', gap: 10,
                            }}
                        >
                            {/* Timeline dot */}
                            <div style={{
                                width: 10, height: 10, borderRadius: '50%',
                                background: meta.color,
                                boxShadow: run.status === 'running' ? `0 0 8px ${meta.color}80` : 'none',
                                flexShrink: 0,
                                animation: run.status === 'running' ? 'pulse 1.5s infinite' : 'none',
                            }} />

                            <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                    <span style={{
                                        fontSize: 11, fontWeight: 700, color: 'var(--text1)',
                                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                    }}>
                                        Run #{runs.length - runIdx}
                                    </span>
                                    <span style={chipStyle(meta.color, `${meta.color}12`)}>
                                        <span style={dotStyle(meta.color, run.status === 'running')} />
                                        {lang === 'zh' ? meta.label_zh : meta.label_en}
                                    </span>
                                    {run.trigger_source && run.trigger_source !== 'ui' && (
                                        <span style={chipStyle()}>
                                            via {run.trigger_source}
                                        </span>
                                    )}
                                </div>
                                <div style={{ display: 'flex', gap: 6, fontSize: 9, color: 'var(--text3)', marginTop: 4, flexWrap: 'wrap' }}>
                                    {run.started_at > 0 && <span style={chipStyle()}>TIME {formatTs(run.started_at, lang)}</span>}
                                    {run.started_at > 0 && <span style={chipStyle()}>DUR {durationStr(run.started_at, run.ended_at)}</span>}
                                    {run.total_tokens > 0 && <span style={chipStyle()}>TOKENS {run.total_tokens.toLocaleString()}</span>}
                                    {run.total_cost > 0 && <span style={chipStyle()}>COST {costStr(run.total_cost)}</span>}
                                    <span style={chipStyle()}>NODES {run.node_execution_ids?.length || 0}</span>
                                    {(run.active_node_execution_ids?.length || 0) > 0 && (
                                        <span style={chipStyle(meta.color, `${meta.color}10`)}>
                                            ACTIVE {run.active_node_execution_ids?.length || 0}
                                        </span>
                                    )}
                                </div>
                            </div>

                            <span style={{
                                fontSize: 10, color: 'var(--text3)',
                                transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
                                transition: 'transform 0.2s',
                            }}>
                                ▼
                            </span>
                        </div>

                        {/* Run Summary */}
                        {run.summary && (
                            <div style={{
                                padding: '0 14px 8px', fontSize: 10, color: 'var(--text2)',
                                lineHeight: 1.5,
                            }}>
                                {run.summary.length > 120 ? run.summary.slice(0, 120) + '…' : run.summary}
                            </div>
                        )}

                        {/* Expanded: Node Executions Timeline */}
                        {isExpanded && (
                            <div style={{
                                borderTop: '1px solid var(--glass-border)',
                                padding: '8px 14px 12px',
                            }}>
                                {(canCancelRun(run.status) || actionError) && (
                                    <div style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        alignItems: 'center',
                                        gap: 8,
                                        marginBottom: 8,
                                        flexWrap: 'wrap',
                                    }}>
                                        <div style={{ fontSize: 9, color: 'var(--text3)' }}>
                                            {tr('运行操作', 'Run Actions')}
                                        </div>
                                        {canCancelRun(run.status) && (
                                            <button
                                                className="btn"
                                                disabled={cancellingRunId === run.id}
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    void handleCancelRun(run.id);
                                                }}
                                                style={{
                                                    fontSize: 10,
                                                    color: '#ef4444',
                                                    borderColor: 'rgba(239, 68, 68, 0.2)',
                                                }}
                                            >
                                                {cancellingRunId === run.id
                                                    ? tr('取消中…', 'Cancelling…')
                                                    : tr('取消运行', 'Cancel Run')}
                                            </button>
                                        )}
                                        {actionError && expandedRunId === run.id && (
                                            <div style={{
                                                width: '100%',
                                                fontSize: 10,
                                                color: '#ef4444',
                                                background: 'rgba(239, 68, 68, 0.06)',
                                                border: '1px solid rgba(239, 68, 68, 0.15)',
                                                borderRadius: 6,
                                                padding: '6px 8px',
                                            }}>
                                                {actionError}
                                            </div>
                                        )}
                                    </div>
                                )}
                                {nodesLoading && expandedRunId === run.id ? (
                                    <div style={{ fontSize: 10, color: 'var(--text3)', textAlign: 'center', padding: 8 }}>
                                        {tr('正在加载节点执行记录…', 'Loading node executions…')}
                                    </div>
                                ) : nodes.length === 0 ? (
                                    <div style={{ fontSize: 10, color: 'var(--text3)', textAlign: 'center', padding: 8 }}>
                                        {tr('暂无节点执行记录', 'No node executions yet')}
                                    </div>
                                ) : (
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
                                        {nodes.map((ne, neIdx) => {
                                            const nMeta = NODE_STATUS_META[ne.status] || NODE_STATUS_META.queued;
                                            const isLast = neIdx === nodes.length - 1;

                                            return (
                                                <div
                                                    key={ne.id}
                                                    onClick={() => onNodeSelect?.(ne)}
                                                    style={{
                                                        display: 'flex', gap: 10, cursor: 'pointer',
                                                        padding: '6px 0',
                                                    }}
                                                >
                                                    {/* Vertical timeline */}
                                                    <div style={{
                                                        display: 'flex', flexDirection: 'column', alignItems: 'center',
                                                        width: 20, flexShrink: 0,
                                                    }}>
                                                        <div style={{
                                                            width: 8, height: 8, borderRadius: '50%',
                                                            background: nMeta.color,
                                                            border: `2px solid ${nMeta.color}40`,
                                                            flexShrink: 0, marginTop: 3,
                                                            boxShadow: ne.status === 'running' ? `0 0 6px ${nMeta.color}60` : 'none',
                                                        }} />
                                                        {!isLast && (
                                                            <div style={{
                                                                width: 1, flex: 1,
                                                                background: `linear-gradient(${nMeta.color}40, var(--glass-border))`,
                                                                marginTop: 2,
                                                            }} />
                                                        )}
                                                    </div>

                                                    {/* Node content */}
                                                    <div style={{ flex: 1, paddingBottom: isLast ? 0 : 4 }}>
                                                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                                                            <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--text1)' }}>
                                                                {ne.node_label || ne.node_key}
                                                            </span>
                                                            <span style={chipStyle(nMeta.color, `${nMeta.color}12`)}>
                                                                <span style={dotStyle(nMeta.color, ne.status === 'running')} />
                                                                {lang === 'zh' ? nMeta.label_zh : nMeta.label_en}
                                                            </span>
                                                            {ne.assigned_model && (
                                                                <span style={chipStyle()}>
                                                                    MODEL {ne.assigned_model}
                                                                </span>
                                                            )}
                                                            {(ne.depends_on_keys?.length || 0) > 0 && (
                                                                <span style={chipStyle('#8b5cf6', 'rgba(139, 92, 246, 0.08)')}>
                                                                    DEPS {ne.depends_on_keys!.join(', ')}
                                                                </span>
                                                            )}
                                                        </div>
                                                        <div style={{ display: 'flex', gap: 6, fontSize: 8, color: 'var(--text3)', marginTop: 4, flexWrap: 'wrap' }}>
                                                            {ne.started_at > 0 && <span style={chipStyle()}>TIME {formatTs(ne.started_at, lang)}</span>}
                                                            {ne.started_at > 0 && <span style={chipStyle()}>DUR {durationStr(ne.started_at, ne.ended_at)}</span>}
                                                            {ne.tokens_used > 0 && <span style={chipStyle()}>TOKENS {ne.tokens_used.toLocaleString()}</span>}
                                                            {ne.cost > 0 && <span style={chipStyle()}>COST {costStr(ne.cost)}</span>}
                                                            {ne.retry_count > 0 && <span style={chipStyle()}>RETRY {ne.retry_count}</span>}
                                                            {ne.retried_from_id && <span style={chipStyle()}>{tr('重试副本', 'Retried')}</span>}
                                                            {(ne.artifact_ids?.length || 0) > 0 && (
                                                                <span style={chipStyle('#4f8fff', 'rgba(79, 143, 255, 0.1)')}>
                                                                    ARTIFACTS {ne.artifact_ids.length}
                                                                </span>
                                                            )}
                                                        </div>
                                                        {/* P2-C: Live progress bar for running nodes */}
                                                        {ne.status === 'running' && (
                                                            <div style={{ marginTop: 4 }}>
                                                                <div style={{
                                                                    height: 3, borderRadius: 2, background: 'var(--glass-border)',
                                                                    overflow: 'hidden', position: 'relative',
                                                                }}>
                                                                    <div style={{
                                                                        height: '100%', borderRadius: 2,
                                                                        background: `linear-gradient(90deg, ${nMeta.color}80, ${nMeta.color})`,
                                                                        width: `${Math.min(100, Math.max(5, ne.progress || 5))}%`,
                                                                        transition: 'width 0.5s ease',
                                                                    }} />
                                                                </div>
                                                                {typeof ne.progress === 'number' && (
                                                                    <span style={{ fontSize: 7, color: nMeta.color, fontWeight: 600 }}>
                                                                        {Math.round(ne.progress)}%
                                                                    </span>
                                                                )}
                                                            </div>
                                                        )}
                                                        {ne.output_summary && (
                                                            <div style={{
                                                                fontSize: 9, color: ne.status === 'running' ? nMeta.color : 'var(--text2)',
                                                                marginTop: 3, lineHeight: 1.4, maxHeight: 40, overflow: 'hidden',
                                                                fontStyle: ne.status === 'running' ? 'italic' : 'normal',
                                                            }}>
                                                                {ne.status === 'running' ? '> ' : ''}
                                                                {ne.output_summary.length > 100
                                                                    ? ne.output_summary.slice(0, 100) + '…'
                                                                    : ne.output_summary}
                                                            </div>
                                                        )}
                                                        {ne.error_message && (
                                                            <div style={{
                                                                fontSize: 9, color: '#ef4444', marginTop: 3,
                                                                background: 'rgba(239,68,68,0.06)', borderRadius: 4,
                                                                padding: '3px 6px', lineHeight: 1.4,
                                                            }}>
                                                                {ne.error_message.length > 100
                                                                    ? ne.error_message.slice(0, 100) + '…'
                                                                    : ne.error_message}
                                                            </div>
                                                        )}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}

                                {/* Risk warnings */}
                                {run.risks?.length > 0 && (
                                    <div style={{
                                        marginTop: 8, padding: '6px 10px', borderRadius: 6,
                                        background: 'rgba(245, 158, 11, 0.06)',
                                        border: '1px solid rgba(245, 158, 11, 0.15)',
                                    }}>
                                        <div style={{ fontSize: 9, fontWeight: 700, color: '#f59e0b', marginBottom: 3 }}>
                                            {tr('风险', 'Risks')}
                                        </div>
                                        {run.risks.map((r, i) => (
                                            <div key={i} style={{ fontSize: 9, color: 'var(--text2)', lineHeight: 1.5 }}>
                                                • {r}
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                );
            })}
        </div>
    );
}
