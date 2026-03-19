'use client';

import { useEffect, useState, useMemo, type CSSProperties } from 'react';
import { type TaskCard, type TaskStatus, type RunReportRecord, type NodeExecutionRecord, type RunRecord, type RunStatus, type SelfCheckItem, type ArtifactRecord, type ReviewDecisionRecord, type ValidationResultRecord, TASK_COLUMNS } from '@/lib/types';
import RunTimeline from './RunTimeline';
import NodeInspectorPanel from './NodeInspectorPanel';
import { useRunContext } from '@/contexts/TaskRunProvider';
import { listArtifacts, launchRun as apiLaunchRun, listWorkflowTemplates, type WorkflowTemplateSummary } from '@/lib/api';

interface TaskDetailPanelProps {
    task: TaskCard;
    lang: 'en' | 'zh';
    onClose: () => void;
    onTransition: (newStatus: TaskStatus) => void;
    onUpdate: (data: Partial<TaskCard>) => Promise<void> | void;
    onRunActivity?: () => Promise<void> | void;
    runReports: RunReportRecord[];
}

const STATUS_TRANSITIONS: Record<TaskStatus, { key: TaskStatus; label_en: string; label_zh: string; variant: string }[]> = {
    backlog:   [{ key: 'planned', label_en: 'Plan', label_zh: '规划', variant: 'primary' }],
    planned:   [{ key: 'executing', label_en: 'Start', label_zh: '开始执行', variant: 'primary' }, { key: 'backlog', label_en: 'Back', label_zh: '退回', variant: '' }],
    executing: [{ key: 'review', label_en: 'Submit Review', label_zh: '提交审核', variant: 'primary' }, { key: 'planned', label_en: 'Back', label_zh: '退回', variant: '' }],
    review:    [{ key: 'selfcheck', label_en: 'Pass → Self-Check', label_zh: '通过 → 自检', variant: 'success' }, { key: 'executing', label_en: 'Reject → Rework', label_zh: '驳回 → 返工', variant: 'danger' }],
    selfcheck: [{ key: 'done', label_en: 'Mark Done', label_zh: '标记完成', variant: 'success' }, { key: 'executing', label_en: 'Fail → Rework', label_zh: '失败 → 返工', variant: 'danger' }],
    done:      [{ key: 'backlog', label_en: 'Reopen', label_zh: '重新打开', variant: '' }],
};

function truncate(text: string, len: number): string {
    return text.length > len ? text.slice(0, len) + '…' : text;
}

function formatDuration(sec: number): string {
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}m ${s}s`;
}

function normalizeTs(v: number): number {
    if (!v) return 0;
    return v < 10_000_000_000 ? v * 1000 : v;
}

function relativeTime(ts: number, lang: string): string {
    if (!ts) return '--';
    const diff = Date.now() - normalizeTs(ts);
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return lang === 'zh' ? '刚刚' : 'just now';
    if (mins < 60) return lang === 'zh' ? `${mins}分钟前` : `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return lang === 'zh' ? `${hours}小时前` : `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return lang === 'zh' ? `${days}天前` : `${days}d ago`;
}

function formatCost(cost: number): string {
    if (!cost) return '';
    return `$${cost.toFixed(4)}`;
}

function formatTokens(tokens: number): string {
    if (!tokens) return '';
    if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
    if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
    return String(tokens);
}

function parseArtifactContent(content: string): Record<string, unknown> | null {
    if (!content.trim()) return null;
    try {
        const parsed = JSON.parse(content);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
            ? parsed as Record<string, unknown>
            : null;
    } catch {
        return null;
    }
}

// Reuse normalizeTs for artifact timestamps
const normalizeArtifactTs = normalizeTs;

function reviewDecisionFromArtifact(artifact: ArtifactRecord): ReviewDecisionRecord | null {
    if (artifact.artifact_type !== 'review_result') return null;
    const payload = parseArtifactContent(artifact.content);
    if (!payload) return null;
    const decision = String(payload.decision || '').trim().toLowerCase();
    if (!decision) return null;
    return {
        id: artifact.id,
        run_id: artifact.run_id,
        node_execution_id: artifact.node_execution_id,
        decision: decision as ReviewDecisionRecord['decision'],
        issues: Array.isArray(payload.issues) ? payload.issues.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : [],
        remaining_risks: Array.isArray(payload.remaining_risks) ? payload.remaining_risks.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : [],
        next_action: String(payload.next_action || '').trim(),
        created_at: artifact.created_at,
    };
}

function validationFromArtifact(artifact: ArtifactRecord): ValidationResultRecord | null {
    if (artifact.artifact_type !== 'report') return null;
    const payload = parseArtifactContent(artifact.content);
    if (!payload) return null;
    const checklist = Array.isArray(payload.checklist)
        ? payload.checklist.reduce<ValidationResultRecord['checklist']>((acc, item) => {
            if (!item || typeof item !== 'object' || Array.isArray(item)) return acc;
            const record = item as Record<string, unknown>;
            const name = String(record.name || '').trim();
            if (!name) return acc;
            const statusValue = String(record.status || '').trim();
            const passedValue = typeof record.passed === 'boolean' ? record.passed : undefined;
            acc.push({
                name,
                status: statusValue || (passedValue === undefined ? 'unknown' : passedValue ? 'passed' : 'failed'),
                detail: String(record.detail || '').trim() || undefined,
            });
            return acc;
        }, [])
        : [];
    const summaryStatus = String(payload.summary_status || '').trim();
    if (!summaryStatus && checklist.length === 0) return null;
    return {
        id: artifact.id,
        run_id: artifact.run_id,
        node_execution_id: artifact.node_execution_id,
        summary_status: (summaryStatus || 'passed') as ValidationResultRecord['summary_status'],
        checklist,
        summary: String(payload.summary || '').trim(),
        created_at: artifact.created_at,
    };
}

function validationChecklistToSelfcheckItems(result: ValidationResultRecord | null): SelfCheckItem[] {
    if (!result) return [];
    return result.checklist.map((item) => ({
        name: item.name,
        passed: item.status === 'passed',
        detail: item.detail || '',
    }));
}

const SECTION_TITLE_STYLE: CSSProperties = {
    fontSize: 10,
    fontWeight: 700,
    color: 'var(--text3)',
    textTransform: 'uppercase',
    letterSpacing: '0.5px',
};

const RUN_STATUS_LABEL: Record<string, Record<RunStatus, string>> = {
    en: { queued: 'Queued', running: 'Running', waiting_review: 'Review', waiting_selfcheck: 'Self-Check', failed: 'Failed', done: 'Done', cancelled: 'Cancelled' },
    zh: { queued: '排队中', running: '运行中', waiting_review: '待审核', waiting_selfcheck: '待自检', failed: '失败', done: '完成', cancelled: '已取消' },
};

const RUN_STATUS_COLOR: Record<RunStatus, string> = {
    queued: '#64748b', running: '#3b82f6', waiting_review: '#f59e0b',
    waiting_selfcheck: '#06b6d4', failed: '#ef4444', done: '#22c55e', cancelled: '#6b7280',
};

const REVIEW_DECISION_CONFIG: Record<string, { label_en: string; label_zh: string; color: string; bg: string; border: string }> = {
    approve:  { label_en: 'APPROVED',   label_zh: '通过',     color: '#22c55e', bg: 'rgba(34,197,94,0.08)',  border: 'rgba(34,197,94,0.2)' },
    approved: { label_en: 'APPROVED',   label_zh: '通过',     color: '#22c55e', bg: 'rgba(34,197,94,0.08)',  border: 'rgba(34,197,94,0.2)' },
    reject:   { label_en: 'REJECTED',   label_zh: '驳回',     color: '#ef4444', bg: 'rgba(239,68,68,0.08)', border: 'rgba(239,68,68,0.2)' },
    rejected: { label_en: 'REJECTED',   label_zh: '驳回',     color: '#ef4444', bg: 'rgba(239,68,68,0.08)', border: 'rgba(239,68,68,0.2)' },
    needs_fix:{ label_en: 'NEEDS FIX',  label_zh: '需要修复', color: '#f59e0b', bg: 'rgba(245,158,11,0.08)',border: 'rgba(245,158,11,0.2)' },
    blocked:  { label_en: 'BLOCKED',    label_zh: '被阻塞',   color: '#a855f7', bg: 'rgba(168,85,247,0.08)',border: 'rgba(168,85,247,0.2)' },
};

const FALLBACK_TEMPLATES: WorkflowTemplateSummary[] = [
    { id: 'simple', label: 'Simple (3 nodes)', description: '', nodeCount: 3 },
    { id: 'standard', label: 'Standard (4 nodes)', description: '', nodeCount: 4 },
    { id: 'pro', label: 'Pro (7 nodes)', description: '', nodeCount: 7 },
];

export default function TaskDetailPanel({ task, lang, onClose, onTransition, onUpdate, onRunActivity, runReports }: TaskDetailPanelProps) {
    const [editingTitle, setEditingTitle] = useState(false);
    const [titleDraft, setTitleDraft] = useState(task.title);
    const [activeTab, setActiveTab] = useState<'overview' | 'runs' | 'review' | 'selfcheck' | 'agents'>('overview');
    const [inspectorNode, setInspectorNode] = useState<NodeExecutionRecord | null>(null);
    const [startingRun, setStartingRun] = useState(false);
    const [selectedTemplate, setSelectedTemplate] = useState('standard');
    const [workflowTemplates, setWorkflowTemplates] = useState<WorkflowTemplateSummary[]>(FALLBACK_TEMPLATES);
    const [actionError, setActionError] = useState('');
    const [tabArtifacts, setTabArtifacts] = useState<ArtifactRecord[]>([]);
    const [tabArtifactsLoading, setTabArtifactsLoading] = useState(false);
    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);

    const { runs, latestRun, fetchRuns, fetchNodeExecutions } = useRunContext();
    const runCount = Math.max(task.runIds?.length || 0, runs.length);

    // ── Run stats ──
    const runStats = useMemo(() => {
        const totalRuns = runs.length;
        const doneRuns = runs.filter(r => r.status === 'done').length;
        const failedRuns = runs.filter(r => r.status === 'failed').length;
        const totalTokens = runs.reduce((sum, r) => sum + (r.total_tokens || 0), 0);
        const totalCost = runs.reduce((sum, r) => sum + (r.total_cost || 0), 0);
        return { totalRuns, doneRuns, failedRuns, totalTokens, totalCost };
    }, [runs]);

    const col = TASK_COLUMNS.find((c) => c.key === task.status);
    const transitions = STATUS_TRANSITIONS[task.status] || [];

    useEffect(() => {
        setTitleDraft(task.title);
    }, [task.id, task.title]);

    useEffect(() => {
        setInspectorNode(null);
        setStartingRun(false);
        setActionError('');
    }, [task.id]);

    useEffect(() => {
        const controller = new AbortController();
        void (async () => {
            try {
                const { templates } = await listWorkflowTemplates();
                if (!controller.signal.aborted && Array.isArray(templates) && templates.length > 0) {
                    setWorkflowTemplates(templates);
                }
            } catch {
                if (!controller.signal.aborted) {
                    setWorkflowTemplates(FALLBACK_TEMPLATES);
                }
            }
        })();
        return () => controller.abort();
    }, []);

    useEffect(() => {
        if (!workflowTemplates.some((tpl) => tpl.id === selectedTemplate)) {
            setSelectedTemplate(workflowTemplates[0]?.id || 'standard');
        }
    }, [selectedTemplate, workflowTemplates]);

    const handleSaveTitle = () => {
        if (titleDraft.trim() && titleDraft.trim() !== task.title) {
            onUpdate({ title: titleDraft.trim() });
        }
        setEditingTitle(false);
    };

    useEffect(() => {
        if (activeTab !== 'review' && activeTab !== 'selfcheck') {
            setTabArtifacts([]);
            setTabArtifactsLoading(false);
            return;
        }
        if (!latestRun?.id) {
            setTabArtifacts([]);
            setTabArtifactsLoading(false);
            return;
        }

        const runId = latestRun.id;
        const controller = new AbortController();
        setTabArtifactsLoading(true);
        void (async () => {
            try {
                const { artifacts } = await listArtifacts(runId, undefined, { signal: controller.signal });
                if (!controller.signal.aborted) {
                    const ordered = [...(artifacts || [])].sort((a, b) => normalizeArtifactTs(b.created_at) - normalizeArtifactTs(a.created_at));
                    setTabArtifacts(ordered);
                }
            } catch {
                if (!controller.signal.aborted) {
                    setTabArtifacts([]);
                }
            } finally {
                if (!controller.signal.aborted) {
                    setTabArtifactsLoading(false);
                }
            }
        })();

        return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeTab, latestRun?.id, task.id]);

    const latestReviewDecision = useMemo(() => {
        for (const artifact of tabArtifacts) {
            const decision = reviewDecisionFromArtifact(artifact);
            if (decision) return decision;
        }
        return null;
    }, [tabArtifacts]);

    const latestValidationResult = useMemo(() => {
        for (const artifact of tabArtifacts) {
            const result = validationFromArtifact(artifact);
            if (result) return result;
        }
        return null;
    }, [tabArtifacts]);

    const reviewVerdict = (latestReviewDecision?.decision || task.reviewVerdict || '').trim().toLowerCase();
    const reviewConfig = REVIEW_DECISION_CONFIG[reviewVerdict] || null;

    const reviewIssues = useMemo(() => {
        return latestReviewDecision?.issues?.length
            ? latestReviewDecision.issues
            : task.reviewIssues || [];
    }, [latestReviewDecision, task.reviewIssues]);

    const remainingRisks = useMemo(() => {
        return latestReviewDecision?.remaining_risks?.length
            ? latestReviewDecision.remaining_risks
            : latestRun?.risks || [];
    }, [latestReviewDecision, latestRun?.risks]);

    const effectiveSelfcheckItems = useMemo(() => {
        return latestValidationResult
            ? validationChecklistToSelfcheckItems(latestValidationResult)
            : task.selfcheckItems || [];
    }, [latestValidationResult, task.selfcheckItems]);

    // Selfcheck stats — derived from stable effectiveSelfcheckItems
    const selfcheckStats = useMemo(() => {
        const total = effectiveSelfcheckItems.length;
        const passed = effectiveSelfcheckItems.filter(i => i.passed).length;
        return { total, passed, allPassed: total > 0 && passed === total };
    }, [effectiveSelfcheckItems]);

    return (
        <div
            style={{
                position: 'fixed', top: 0, right: 0, bottom: 0,
                width: 480, maxWidth: '100vw', zIndex: 9500,
                background: 'var(--surface-strong)', borderLeft: '1px solid var(--glass-border)',
                boxShadow: '-8px 0 32px rgba(0,0,0,0.3)',
                display: 'flex', flexDirection: 'column',
                animation: 'slideInRight 0.2s ease-out',
            }}
            onClick={(e) => e.stopPropagation()}
        >
            {/* Header */}
            <div style={{
                padding: '14px 16px', borderBottom: '1px solid var(--glass-border)',
                display: 'flex', alignItems: 'flex-start', gap: 10,
            }}>
                <div style={{ flex: 1 }}>
                    {/* Status Badge */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                        <span style={{
                            padding: '2px 8px', borderRadius: 6, fontSize: 10, fontWeight: 700,
                            background: `${col?.color || '#64748b'}18`,
                            color: col?.color || '#64748b',
                            border: `1px solid ${col?.color || '#64748b'}30`,
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: 6,
                        }}>
                            <span style={{
                                width: 6,
                                height: 6,
                                borderRadius: '50%',
                                background: col?.color || '#64748b',
                                boxShadow: `0 0 6px ${(col?.color || '#64748b')}40`,
                            }} />
                            {lang === 'zh' ? col?.label_zh : col?.label_en}
                        </span>
                        <span style={{
                            padding: '2px 6px', borderRadius: 4, fontSize: 9,
                            background: task.mode === 'pro' ? 'rgba(168, 85, 247, 0.12)' : 'var(--glass)',
                            color: task.mode === 'pro' ? '#a855f7' : 'var(--text3)',
                        }}>
                            {(task.mode || 'standard').toUpperCase()}
                        </span>
                        <span style={{
                            padding: '2px 6px', borderRadius: 4, fontSize: 9,
                            background: 'var(--glass)',
                            color: task.priority === 'urgent' ? '#ef4444' : task.priority === 'high' ? '#f59e0b' : 'var(--text3)',
                        }}>
                            {task.priority.toUpperCase()}
                        </span>
                    </div>

                    {/* Title */}
                    {editingTitle ? (
                        <input
                            className="s-input"
                            value={titleDraft}
                            onChange={(e) => setTitleDraft(e.target.value)}
                            onBlur={handleSaveTitle}
                            onKeyDown={(e) => e.key === 'Enter' && handleSaveTitle()}
                            autoFocus
                            style={{ fontSize: 14, fontWeight: 700, width: '100%' }}
                        />
                    ) : (
                        <h3
                            onClick={() => { setEditingTitle(true); setTitleDraft(task.title); }}
                            style={{
                                fontSize: 14, fontWeight: 700, color: 'var(--text1)',
                                cursor: 'pointer', lineHeight: 1.3,
                            }}
                            title={tr('点击编辑标题', 'Click to edit title')}
                        >
                            {task.title}
                        </h3>
                    )}

                    {/* Owner + Time */}
                    <div style={{ fontSize: 9, color: 'var(--text3)', marginTop: 4 }}>
                    {task.owner && <span>{task.owner} · </span>}
                    {runCount > 0 && <span>{runCount} {tr('次运行', 'runs')} · </span>}
                    {task.relatedFiles?.length > 0 && <span>{task.relatedFiles.length} {tr('个文件', 'files')}</span>}
                    </div>
                </div>
                <button className="modal-close" onClick={onClose} style={{ marginTop: 2 }}>✕</button>
            </div>

            {/* Progress */}
            <div style={{ padding: '0 16px' }}>
                <div className="progress-bar" style={{ height: 3, marginTop: 0 }}>
                    <div className="fill" style={{ width: `${task.progress}%` }} />
                </div>
                <div style={{ fontSize: 9, color: 'var(--text3)', textAlign: 'right', marginTop: 2 }}>
                    {task.progress}%
                </div>
            </div>

            {/* Tabs */}
            <div className="settings-tabs" style={{ padding: '0 12px' }}>
                {[
                    { key: 'overview' as const, label: tr('概览', 'Overview') },
                    { key: 'runs' as const, label: tr(`运行 (${runCount})`, `Runs (${runCount})`) },
                    { key: 'review' as const, label: tr('审核', 'Review') },
                    { key: 'selfcheck' as const, label: tr('自检', 'Self-Check') },
                    ...(task.mode === 'pro' ? [{ key: 'agents' as const, label: tr('多代理', 'Agents') }] : []),
                ].map((tab) => (
                    <button
                        key={tab.key}
                        className={`settings-tab ${activeTab === tab.key ? 'active' : ''}`}
                        onClick={() => setActiveTab(tab.key)}
                    >
                        {tab.label}
                    </button>
                ))}
            </div>

            {/* Tab Content */}
            <div style={{ flex: 1, overflow: 'auto', padding: 16 }}>
                {activeTab === 'overview' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        {/* Description */}
                        <div className="s-section">
                            <div className="s-section-title">{tr('描述', 'Description')}</div>
                            <p style={{ fontSize: 11, color: 'var(--text2)', lineHeight: 1.6 }}>
                                {task.description || tr('暂无描述', 'No description')}
                            </p>
                        </div>

                        {/* Latest Summary */}
                        {task.latestSummary && (
                            <div className="s-section">
                                <div className="s-section-title">{tr('最近摘要', 'Latest Summary')}</div>
                                <p style={{
                                    fontSize: 11, color: 'var(--text2)', lineHeight: 1.6,
                                    background: 'var(--glass)', borderRadius: 8, padding: '8px 10px',
                                    border: '1px solid var(--glass-border)',
                                }}>
                                    {task.latestSummary}
                                </p>
                            </div>
                        )}

                        {/* Latest Risk */}
                        {task.latestRisk && (
                            <div className="s-section">
                                <div className="s-section-title">{tr('风险', 'Risks')}</div>
                                <p style={{
                                    fontSize: 11, color: '#f59e0b', lineHeight: 1.6,
                                    background: 'rgba(245, 158, 11, 0.06)', borderRadius: 8,
                                    padding: '8px 10px', border: '1px solid rgba(245, 158, 11, 0.15)',
                                }}>
                                    {task.latestRisk}
                                </p>
                            </div>
                        )}

                        {/* Related Files */}
                        {task.relatedFiles?.length > 0 && (
                            <div className="s-section">
                                <div className="s-section-title">{tr('关联文件', 'Related Files')}</div>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                                    {task.relatedFiles.map((f, i) => (
                                        <div key={i} style={{
                                            fontSize: 10, color: 'var(--text2)',
                                            padding: '3px 8px', borderRadius: 4,
                                            background: 'var(--glass)',
                                            fontFamily: 'var(--font-mono), monospace',
                                            wordBreak: 'break-all',
                                        }}>
                                            {f}
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}

                        {/* P2-D: Latest Run completion summary */}
                        {latestRun && latestRun.status === 'done' && (
                            <div className="s-section">
                                <div className="s-section-title">{tr('最近运行结果', 'Latest Run Result')}</div>
                                <div style={{
                                    borderRadius: 8, padding: '10px 12px',
                                    background: 'rgba(34, 197, 94, 0.06)',
                                    border: '1px solid rgba(34, 197, 94, 0.15)',
                                }}>
                                    <div style={{ display: 'flex', gap: 16, marginBottom: 6, flexWrap: 'wrap' }}>
                                        <span style={{ fontSize: 10, color: '#22c55e', fontWeight: 700 }}>
                                            {tr('运行完成', 'Run Completed')}
                                        </span>
                                        {(latestRun.total_tokens ?? 0) > 0 && (
                                            <span style={{ fontSize: 10, color: 'var(--text3)' }}>
                                                {(latestRun.total_tokens ?? 0).toLocaleString()} tokens
                                            </span>
                                        )}
                                        {(latestRun.total_cost ?? 0) > 0 && (
                                            <span style={{ fontSize: 10, color: 'var(--text3)' }}>
                                                ${(latestRun.total_cost ?? 0).toFixed(4)}
                                            </span>
                                        )}
                                        {latestRun.runtime && (
                                            <span style={{ fontSize: 10, color: 'var(--text3)' }}>
                                                {latestRun.runtime}
                                            </span>
                                        )}
                                    </div>
                                    {latestRun.summary && (
                                        <p style={{
                                            fontSize: 11, color: 'var(--text2)', lineHeight: 1.5, margin: 0,
                                        }}>
                                            {latestRun.summary}
                                        </p>
                                    )}
                                    {(latestRun.risks?.length ?? 0) > 0 && (
                                        <div style={{ marginTop: 6 }}>
                                            <span style={{ fontSize: 9, color: '#f59e0b', fontWeight: 600 }}>
                                                {tr('风险', 'Risks')}:
                                            </span>
                                            {latestRun.risks!.map((r: string, i: number) => (
                                                <span key={i} style={{
                                                    fontSize: 9, color: '#f59e0b', marginLeft: 4,
                                                }}>
                                                    • {r}
                                                </span>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}
                    </div>
                )}

                {activeTab === 'runs' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        {/* Run Stats Header */}
                        {runStats.totalRuns > 0 && (
                            <div style={{
                                display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(80px, 1fr))',
                                gap: 6, padding: '8px 10px', borderRadius: 8,
                                background: 'var(--glass)', border: '1px solid var(--glass-border)',
                            }}>
                                <div style={{ textAlign: 'center' }}>
                                    <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--text1)' }}>{runStats.totalRuns}</div>
                                    <div style={{ fontSize: 8, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{tr('总运行', 'Total Runs')}</div>
                                </div>
                                <div style={{ textAlign: 'center' }}>
                                    <div style={{ fontSize: 16, fontWeight: 700, color: '#22c55e' }}>{runStats.doneRuns}</div>
                                    <div style={{ fontSize: 8, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{tr('成功', 'Passed')}</div>
                                </div>
                                <div style={{ textAlign: 'center' }}>
                                    <div style={{ fontSize: 16, fontWeight: 700, color: runStats.failedRuns > 0 ? '#ef4444' : 'var(--text3)' }}>{runStats.failedRuns}</div>
                                    <div style={{ fontSize: 8, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{tr('失败', 'Failed')}</div>
                                </div>
                                {runStats.totalTokens > 0 && (
                                    <div style={{ textAlign: 'center' }}>
                                        <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--text1)' }}>{formatTokens(runStats.totalTokens)}</div>
                                        <div style={{ fontSize: 8, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{tr('令牌', 'Tokens')}</div>
                                    </div>
                                )}
                                {runStats.totalCost > 0 && (
                                    <div style={{ textAlign: 'center' }}>
                                        <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--text1)' }}>{formatCost(runStats.totalCost)}</div>
                                        <div style={{ fontSize: 8, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{tr('费用', 'Cost')}</div>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* Latest Run Badge */}
                        {latestRun && (
                            <div style={{
                                display: 'flex', alignItems: 'center', gap: 8,
                                padding: '6px 10px', borderRadius: 6,
                                background: `${RUN_STATUS_COLOR[latestRun.status] || '#64748b'}10`,
                                border: `1px solid ${RUN_STATUS_COLOR[latestRun.status] || '#64748b'}25`,
                            }}>
                                <span style={{
                                    width: 6, height: 6, borderRadius: '50%',
                                    background: RUN_STATUS_COLOR[latestRun.status] || '#64748b',
                                    boxShadow: latestRun.status === 'running' ? `0 0 6px ${RUN_STATUS_COLOR.running}` : 'none',
                                    animation: latestRun.status === 'running' ? 'pulse 1.5s infinite' : 'none',
                                }} />
                                <span style={{ fontSize: 10, fontWeight: 600, color: RUN_STATUS_COLOR[latestRun.status] || '#64748b' }}>
                                    {tr('最新', 'Latest')}: {(RUN_STATUS_LABEL[lang] || RUN_STATUS_LABEL.en)[latestRun.status] || latestRun.status}
                                </span>
                                <span style={{ fontSize: 9, color: 'var(--text3)', marginLeft: 'auto' }}>
                                    {latestRun.summary ? truncate(latestRun.summary, 40) : relativeTime(latestRun.started_at, lang)}
                                </span>
                            </div>
                        )}

                        {/* V1 Run Timeline */}
                        <RunTimeline
                            taskId={task.id}
                            lang={lang}
                            onRunsChanged={onRunActivity}
                            onNodeSelect={(ne) => {
                                setInspectorNode(ne);
                            }}
                            onRunSelect={() => {}}
                        />

                        {/* Legacy reports fallback */}
                        {runReports.length > 0 && (
                            <details style={{ marginTop: 4 }}>
                                <summary style={{
                                    fontSize: 10, color: 'var(--text3)', cursor: 'pointer',
                                    padding: '6px 10px', borderRadius: 6,
                                    background: 'var(--glass)',
                                }}>
                                    {tr('旧版运行报告', 'Legacy Run Reports')} ({runReports.length})
                                </summary>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
                                    {runReports.map((report) => (
                                        <div key={report.id} style={{
                                            padding: '10px 12px', borderRadius: 8,
                                            background: 'var(--glass)', border: '1px solid var(--glass-border)',
                                        }}>
                                            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                                                <span style={{
                                                    width: 6, height: 6, borderRadius: '50%',
                                                    background: report.success ? '#22c55e' : '#ef4444',
                                                }} />
                                                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text1)', flex: 1 }}>
                                                    {truncate(report.goal, 60)}
                                                </span>
                                            </div>
                                            <div style={{ display: 'flex', gap: 12, fontSize: 9, color: 'var(--text3)' }}>
                                                <span>{report.completed}/{report.totalSubtasks} {tr('完成', 'done')}</span>
                                                {report.totalRetries > 0 && <span>{report.totalRetries} {tr('重试', 'retries')}</span>}
                                                <span>{formatDuration(report.durationSeconds)}</span>
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </details>
                        )}
                    </div>
                )}

                {activeTab === 'review' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        {/* Review Decision Card */}
                        <div className="s-section">
                            <div className="s-section-title" style={SECTION_TITLE_STYLE}>
                                {tr('审核结果', 'Review Verdict')}
                            </div>
                            {tabArtifactsLoading ? (
                                <div style={{
                                    padding: '12px 14px', borderRadius: 8,
                                    background: 'var(--glass)', border: '1px solid var(--glass-border)',
                                    color: 'var(--text3)', fontSize: 10,
                                }}>
                                    {tr('正在加载审核结果…', 'Loading review result…')}
                                </div>
                            ) : reviewConfig ? (
                                <div style={{
                                    padding: '12px 14px', borderRadius: 8,
                                    background: reviewConfig.bg,
                                    border: `1px solid ${reviewConfig.border}`,
                                    display: 'flex', alignItems: 'center', gap: 10,
                                }}>
                                    {/* Status dot */}
                                    <span style={{
                                        width: 10, height: 10, borderRadius: '50%',
                                        background: reviewConfig.color,
                                        boxShadow: `0 0 8px ${reviewConfig.color}40`,
                                        flexShrink: 0,
                                    }} />
                                    <div style={{ flex: 1 }}>
                                        <div style={{ fontSize: 13, fontWeight: 700, color: reviewConfig.color }}>
                                            {lang === 'zh' ? reviewConfig.label_zh : reviewConfig.label_en}
                                        </div>
                                        <div style={{ fontSize: 9, color: 'var(--text3)', marginTop: 2 }}>
                                            {tr('由代理自动审核', 'Reviewed by agent')}
                                        </div>
                                        {latestReviewDecision?.next_action && (
                                            <div style={{ fontSize: 9, color: 'var(--text2)', marginTop: 4 }}>
                                                {tr('下一步', 'Next')}: {latestReviewDecision.next_action}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            ) : (
                                <div style={{
                                    padding: '20px 14px', borderRadius: 8,
                                    background: 'var(--glass)', border: '1px dashed var(--glass-border)',
                                    color: 'var(--text3)', fontSize: 11, textAlign: 'center',
                                }}>
                                    <div style={{ fontSize: 22, marginBottom: 6, opacity: 0.4 }}>—</div>
                                    {tr('任务流进入审核阶段后，审核结果将显示在此处', 'Review results will appear here once the task enters review stage')}
                                </div>
                            )}
                        </div>

                        {/* Issues Found */}
                        {reviewIssues.length > 0 && (
                            <div className="s-section">
                                <div className="s-section-title" style={SECTION_TITLE_STYLE}>
                                    {tr('发现问题', 'Issues Found')} ({reviewIssues.length})
                                </div>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                    {reviewIssues.map((issue, i) => (
                                        <div key={i} style={{
                                            fontSize: 10, color: 'var(--text2)',
                                            padding: '6px 10px', borderRadius: 6,
                                            background: 'rgba(239, 68, 68, 0.04)',
                                            border: '1px solid rgba(239, 68, 68, 0.1)',
                                            display: 'flex', alignItems: 'flex-start', gap: 6,
                                            lineHeight: 1.5,
                                        }}>
                                            <span style={{
                                                color: 'var(--text3)', fontSize: 9, fontWeight: 700,
                                                minWidth: 16, textAlign: 'right', flexShrink: 0,
                                                fontFamily: 'var(--font-mono, monospace)',
                                            }}>
                                                #{i + 1}
                                            </span>
                                            <span>{issue}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}

                        {/* Latest Run Review Context */}
                        {latestRun && (latestRun.status === 'waiting_review' || remainingRisks.length > 0) && (
                            <div className="s-section">
                                <div className="s-section-title" style={SECTION_TITLE_STYLE}>
                                    {tr('剩余风险', 'Remaining Risks')}
                                </div>
                                {remainingRisks.length > 0 ? (
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                        {remainingRisks.map((risk, i) => (
                                            <div key={i} style={{
                                                fontSize: 10, color: '#f59e0b',
                                                padding: '5px 10px', borderRadius: 6,
                                                background: 'rgba(245, 158, 11, 0.05)',
                                                border: '1px solid rgba(245, 158, 11, 0.12)',
                                                display: 'flex', alignItems: 'flex-start', gap: 6,
                                            }}>
                                                <span style={{
                                                    width: 6,
                                                    height: 6,
                                                    borderRadius: '50%',
                                                    background: '#f59e0b',
                                                    marginTop: 4,
                                                    flexShrink: 0,
                                                }} />
                                                <span>{risk}</span>
                                            </div>
                                        ))}
                                    </div>
                                ) : (
                                    <div style={{
                                        padding: '8px 10px', borderRadius: 6,
                                        background: 'rgba(34, 197, 94, 0.05)',
                                        border: '1px solid rgba(34, 197, 94, 0.12)',
                                        fontSize: 10, color: '#22c55e', textAlign: 'center',
                                    }}>
                                        {tr('未发现剩余风险', 'No remaining risks identified')}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                )}

                {activeTab === 'selfcheck' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        {/* Progress Summary */}
                        {tabArtifactsLoading ? (
                            <div style={{
                                padding: '10px 14px', borderRadius: 8,
                                background: 'var(--glass)', border: '1px solid var(--glass-border)',
                                color: 'var(--text3)', fontSize: 10,
                            }}>
                                {tr('正在加载自检结果…', 'Loading self-check result…')}
                            </div>
                        ) : selfcheckStats.total > 0 && (
                            <div style={{
                                padding: '10px 14px', borderRadius: 8,
                                background: 'var(--glass)', border: '1px solid var(--glass-border)',
                                display: 'flex', alignItems: 'center', gap: 12,
                            }}>
                                {/* Progress fraction */}
                                <div style={{ textAlign: 'center', minWidth: 50 }}>
                                    <div style={{ fontSize: 18, fontWeight: 700, color: selfcheckStats.allPassed ? '#22c55e' : '#f59e0b' }}>
                                        {selfcheckStats.passed}/{selfcheckStats.total}
                                    </div>
                                    <div style={{ fontSize: 8, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                                        {tr('通过', 'Passed')}
                                    </div>
                                </div>
                                {/* Progress bar */}
                                <div style={{ flex: 1 }}>
                                    <div style={{
                                        height: 4, borderRadius: 2,
                                        background: 'rgba(255,255,255,0.06)',
                                        overflow: 'hidden',
                                    }}>
                                        <div style={{
                                            height: '100%', borderRadius: 2,
                                            width: `${selfcheckStats.total > 0 ? (selfcheckStats.passed / selfcheckStats.total) * 100 : 0}%`,
                                            background: selfcheckStats.allPassed
                                                ? 'linear-gradient(90deg, #22c55e, #4ade80)'
                                                : 'linear-gradient(90deg, #f59e0b, #fbbf24)',
                                            transition: 'width 0.3s ease',
                                        }} />
                                    </div>
                                    <div style={{
                                        fontSize: 9, color: selfcheckStats.allPassed ? '#22c55e' : '#f59e0b',
                                        fontWeight: 600, marginTop: 4,
                                    }}>
                                        {selfcheckStats.allPassed
                                            ? tr('全部通过 — 可标记完成', 'All passed — ready to mark done')
                                            : tr('部分未通过 — 需要修复', 'Some failed — fixes required')}
                                    </div>
                                    {latestValidationResult?.summary && (
                                        <div style={{ fontSize: 9, color: 'var(--text2)', marginTop: 4, lineHeight: 1.5 }}>
                                            {latestValidationResult.summary}
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}

                        {/* Checklist */}
                        <div className="s-section">
                            <div className="s-section-title" style={SECTION_TITLE_STYLE}>
                                {tr('检查项', 'Checklist')}
                            </div>
                            {effectiveSelfcheckItems.length > 0 ? (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                    {effectiveSelfcheckItems.map((item, i) => (
                                        <div key={i} style={{
                                            display: 'flex', alignItems: 'flex-start', gap: 8,
                                            padding: '7px 10px', borderRadius: 6,
                                            background: item.passed
                                                ? 'rgba(34, 197, 94, 0.04)' : 'rgba(239, 68, 68, 0.04)',
                                            border: `1px solid ${item.passed
                                                ? 'rgba(34, 197, 94, 0.12)' : 'rgba(239, 68, 68, 0.12)'}`,
                                        }}>
                                            {/* Pass/Fail dot */}
                                            <span style={{
                                                width: 8, height: 8, borderRadius: '50%', marginTop: 3,
                                                background: item.passed ? '#22c55e' : '#ef4444',
                                                boxShadow: `0 0 4px ${item.passed ? 'rgba(34,197,94,0.4)' : 'rgba(239,68,68,0.4)'}`,
                                                flexShrink: 0,
                                            }} />
                                            <div style={{ flex: 1, minWidth: 0 }}>
                                                <div style={{
                                                    fontSize: 11, fontWeight: 600,
                                                    color: item.passed ? 'var(--text1)' : '#ef4444',
                                                }}>
                                                    {item.name}
                                                </div>
                                                {item.detail && (
                                                    <div style={{
                                                        fontSize: 9, color: 'var(--text3)', marginTop: 2,
                                                        lineHeight: 1.4,
                                                    }}>
                                                        {item.detail}
                                                    </div>
                                                )}
                                            </div>
                                            <span style={{
                                                fontSize: 8, fontWeight: 700, textTransform: 'uppercase',
                                                color: item.passed ? '#22c55e' : '#ef4444',
                                                padding: '2px 5px', borderRadius: 3,
                                                background: item.passed ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)',
                                                whiteSpace: 'nowrap', alignSelf: 'center',
                                            }}>
                                                {item.passed ? tr('通过', 'PASS') : tr('失败', 'FAIL')}
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            ) : (
                                <div style={{
                                    padding: '20px 14px', borderRadius: 8,
                                    background: 'var(--glass)', border: '1px dashed var(--glass-border)',
                                    color: 'var(--text3)', fontSize: 11, textAlign: 'center',
                                }}>
                                    <div style={{ fontSize: 22, marginBottom: 6, opacity: 0.4 }}>—</div>
                                    {tr('运行完成后自检数据将自动填充',
                                        'Self-check data populates after a run completes')}
                                </div>
                            )}
                        </div>
                    </div>
                )}

                {activeTab === 'agents' && task.mode === 'pro' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        <div className="s-section">
                            <div className="s-section-title" style={SECTION_TITLE_STYLE}>{tr('多代理协作', 'Agent Collaboration')}</div>
                        </div>
                        {/* Show agent info from the latest run */}
                        {runReports.length > 0 ? (
                            runReports[0].subtasks.map((st) => (
                                <div key={st.id} style={{
                                    padding: '8px 10px', borderRadius: 8,
                                    background: 'var(--glass)', border: '1px solid var(--glass-border)',
                                    display: 'flex', alignItems: 'center', gap: 8,
                                }}>
                                    <span style={{
                                        width: 8, height: 8, borderRadius: '50%',
                                        background: st.status === 'completed' ? '#22c55e'
                                            : st.status === 'failed' ? '#ef4444' : '#f59e0b',
                                        flexShrink: 0,
                                    }} />
                                    <div style={{ flex: 1 }}>
                                        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text1)' }}>
                                            #{st.id} {st.agent}
                                        </div>
                                        <div style={{ fontSize: 9, color: 'var(--text3)' }}>
                                            {st.status} {st.durationSeconds ? `· ${formatDuration(st.durationSeconds)}` : ''}
                                            {st.retries > 0 ? ` · ${st.retries} retries` : ''}
                                        </div>
                                        {st.outputPreview && (
                                            <div style={{
                                                fontSize: 9, color: 'var(--text2)', marginTop: 3,
                                                background: 'rgba(0,0,0,0.15)', borderRadius: 4,
                                                padding: '3px 6px', maxHeight: 60, overflow: 'hidden',
                                                fontFamily: 'var(--font-mono), monospace',
                                            }}>
                                                {truncate(st.outputPreview, 200)}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            ))
                        ) : (
                            <div style={{
                                padding: 16, textAlign: 'center', color: 'var(--text3)', fontSize: 11,
                            }}>
                                {tr('运行Pro模式任务后显示代理详情', 'Run a Pro mode task to see agent details')}
                            </div>
                        )}
                    </div>
                )}
            </div>

            {/* Actions */}
            <div style={{
                padding: '10px 16px',
                borderTop: '1px solid var(--glass-border)',
                display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap',
            }}>
                {actionError && (
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
                {transitions.map((t) => (
                    <button
                        key={t.key}
                        className={`btn ${t.variant ? `btn-${t.variant}` : ''}`}
                        onClick={() => onTransition(t.key)}
                        style={{ fontSize: 11 }}
                    >
                        {lang === 'zh' ? t.label_zh : t.label_en}
                    </button>
                ))}
                <select
                    value={selectedTemplate}
                    onChange={(e) => setSelectedTemplate(e.target.value)}
                    style={{
                        fontSize: 11, padding: '4px 6px', borderRadius: 4,
                        background: 'var(--bg2)', color: 'var(--text1)', border: '1px solid var(--border1)',
                    }}
                >
                    {workflowTemplates.map((template) => (
                        <option key={template.id} value={template.id}>
                            {template.label}
                        </option>
                    ))}
                </select>
                <button
                    className="btn btn-primary"
                    disabled={startingRun}
                    onClick={async () => {
                        try {
                            setStartingRun(true);
                            setActionError('');
                            const result = await apiLaunchRun({
                                task_id: task.id,
                                template_id: selectedTemplate,
                                runtime: 'openclaw',
                                trigger_source: 'ui',
                            });
                            if (!result?.success) throw new Error('Failed to launch run');
                            // Refresh run list and node data
                            await fetchRuns();
                            if (result.run?.id) {
                                await fetchNodeExecutions(result.run.id);
                            }
                            await onRunActivity?.();
                            setActiveTab('runs');
                        } catch (e) {
                            console.error('[TaskDetail] Failed to launch run:', e);
                            setActionError(e instanceof Error
                                ? e.message
                                : tr('启动运行失败，请稍后重试', 'Failed to start run. Please try again.'));
                        } finally {
                            setStartingRun(false);
                        }
                    }}
                    style={{ fontSize: 11 }}
                >
                    {startingRun ? tr('启动中…', 'Starting…') : tr('启动运行', 'Start Run')}
                </button>
            </div>

            {/* Node Inspector Panel */}
            {inspectorNode && (
                <NodeInspectorPanel
                    nodeExecution={inspectorNode}
                    lang={lang}
                    onClose={() => setInspectorNode(null)}
                    onRetry={(newNe) => {
                        // Switch inspector to the new (retried) node execution
                        setInspectorNode(newNe);
                        void onRunActivity?.();
                    }}
                />
            )}
        </div>
    );
}
