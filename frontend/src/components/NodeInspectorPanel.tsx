'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import type { NodeExecutionRecord, ArtifactRecord, NodeExecutionStatus } from '@/lib/types';
import { listArtifacts, getNodeExecution, retryNodeExecution } from '@/lib/api';
import { buildReadableCurrentWork, formatSkillLabel, getStructuredOutputSections } from '@/lib/nodeOutputHumanizer';

interface NodeInspectorPanelProps {
    nodeExecution: NodeExecutionRecord;
    lang: 'en' | 'zh';
    onClose: () => void;
    onRetry?: (newNodeExec: NodeExecutionRecord) => void;
}

const MAX_NODE_RETRY_COUNT = 5;

const STATUS_META: Record<NodeExecutionStatus, { color: string; label_en: string; label_zh: string }> = {
    queued:            { color: '#64748b', label_en: 'Queued',      label_zh: '排队' },
    running:           { color: '#3b82f6', label_en: 'Running',     label_zh: '运行中' },
    passed:            { color: '#22c55e', label_en: 'Passed',      label_zh: '通过' },
    failed:            { color: '#ef4444', label_en: 'Failed',      label_zh: '失败' },
    blocked:           { color: '#f59e0b', label_en: 'Blocked',     label_zh: '阻塞' },
    waiting_approval:  { color: '#a855f7', label_en: 'Awaiting Approval', label_zh: '等待审批' },
    skipped:           { color: '#6b7280', label_en: 'Skipped',     label_zh: '已跳过' },
    cancelled:         { color: '#78716c', label_en: 'Cancelled',   label_zh: '已取消' },
};

const ARTIFACT_TYPE_META: Record<string, { mark: string; label_en: string; label_zh: string }> = {
    changed_files:    { mark: 'CF', label_en: 'Changed Files',    label_zh: '变更文件' },
    diff_summary:     { mark: 'DI', label_en: 'Diff Summary',     label_zh: '差异摘要' },
    report:           { mark: 'RP', label_en: 'Report',           label_zh: '报告' },
    review_result:    { mark: 'RV', label_en: 'Review Result',    label_zh: '审核结果' },
    test_output:      { mark: 'TS', label_en: 'Test Output',      label_zh: '测试输出' },
    build_output:     { mark: 'BD', label_en: 'Build Output',     label_zh: '构建输出' },
    run_summary:      { mark: 'RS', label_en: 'Run Summary',      label_zh: '运行摘要' },
    risk_report:      { mark: 'RK', label_en: 'Risk Report',      label_zh: '风险报告' },
    deployment_notes: { mark: 'DP', label_en: 'Deploy Notes',     label_zh: '部署说明' },
    raw_log:          { mark: 'LG', label_en: 'Raw Log',           label_zh: '原始日志' },
    preview_ref:      { mark: 'PV', label_en: 'Preview',          label_zh: '预览' },
};

function formatTs(epochSec: number, lang: 'en' | 'zh'): string {
    if (!epochSec) return '--';
    const d = new Date(epochSec < 1e12 ? epochSec * 1000 : epochSec);
    return d.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
}

function durationStr(startSec: number, endSec: number): string {
    if (!startSec) return '--';
    const end = endSec || Date.now() / 1000;
    const diff = Math.max(0, Math.round(end - startSec));
    if (diff < 60) return `${diff}s`;
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    return `${m}m ${s}s`;
}

export default function NodeInspectorPanel({ nodeExecution: initNe, lang, onClose, onRetry }: NodeInspectorPanelProps) {
    const [ne, setNe] = useState<NodeExecutionRecord>(initNe);
    const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
    const [artLoading, setArtLoading] = useState(false);
    const [expandedArt, setExpandedArt] = useState<string | null>(null);
    const [retrying, setRetrying] = useState(false);
    const [retryError, setRetryError] = useState('');
    const currentNodeIdRef = useRef(initNe.id);

    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);
    const sMeta = STATUS_META[ne.status] || STATUS_META.queued;
    const retryLimitReached = ne.retry_count >= MAX_NODE_RETRY_COUNT;
    const retryBudgetRemaining = Math.max(0, MAX_NODE_RETRY_COUNT - ne.retry_count);
    const readableOutputSummary = ne.output_summary
        ? buildReadableCurrentWork({
            lang,
            nodeType: ne.node_key || 'builder',
            status: ne.status,
            phase: ne.phase || '',
            taskDescription: ne.input_summary || '',
            loadedSkills: Array.isArray(ne.loaded_skills) ? ne.loaded_skills : [],
            outputSummary: ne.output_summary,
            lastOutput: ne.output_summary,
            logs: Array.isArray(ne.activity_log) ? ne.activity_log : [],
        })
        : '';
    const outputSections = ne.output_summary
        ? getStructuredOutputSections(ne.output_summary, {
            lang,
            nodeType: ne.node_key || 'builder',
            status: ne.status,
        })
        : [];
    const loadedSkills = Array.isArray(ne.loaded_skills) ? ne.loaded_skills : [];
    const activityLog = Array.isArray(ne.activity_log) ? ne.activity_log : [];
    const referenceUrls = Array.isArray(ne.reference_urls) ? ne.reference_urls : [];

    useEffect(() => {
        currentNodeIdRef.current = initNe.id;
        setNe(initNe);
        setArtifacts([]);
        setArtLoading(false);
        setExpandedArt(null);
        setRetryError('');
    }, [initNe]);

    // Refresh node execution data
    const refreshNode = useCallback(async (nodeId: string) => {
        try {
            const { nodeExecution } = await getNodeExecution(nodeId);
            if (nodeExecution && currentNodeIdRef.current === nodeId) {
                setNe(nodeExecution);
            }
        } catch { /* ignore */ }
    }, []);

    // Fetch artifacts
    const fetchArtifacts = useCallback(async (nodeId: string) => {
        try {
            setArtLoading(true);
            const { artifacts: fetched } = await listArtifacts(undefined, nodeId);
            if (currentNodeIdRef.current === nodeId) {
                setArtifacts(fetched || []);
            }
        } catch { /* ignore */ }
        finally {
            if (currentNodeIdRef.current === nodeId) {
                setArtLoading(false);
            }
        }
    }, []);

    useEffect(() => {
        void refreshNode(initNe.id);
        void fetchArtifacts(initNe.id);
    }, [initNe.id, refreshNode, fetchArtifacts]);

    // Poll while running
    useEffect(() => {
        if (ne.status !== 'running') return;
        const timer = window.setInterval(() => {
            void refreshNode(ne.id);
            void fetchArtifacts(ne.id);
        }, 3000);
        return () => window.clearInterval(timer);
    }, [ne.id, ne.status, refreshNode, fetchArtifacts]);

    useEffect(() => {
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [onClose]);

    return (
        <div
            style={{
                position: 'fixed', top: 0, right: 0, bottom: 0,
                width: 420, maxWidth: '100vw', zIndex: 9600,
                background: 'var(--surface-strong)',
                borderLeft: '1px solid var(--glass-border)',
                boxShadow: '-8px 0 32px rgba(0,0,0,0.35)',
                display: 'flex', flexDirection: 'column',
                animation: 'slideInRight 0.2s ease-out',
            }}
            onClick={(e) => e.stopPropagation()}
        >
            {/* Header */}
            <div style={{
                padding: '14px 16px',
                borderBottom: '1px solid var(--glass-border)',
                display: 'flex', alignItems: 'flex-start', gap: 10,
            }}>
                <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                        <span style={{
                            padding: '2px 8px', borderRadius: 6, fontSize: 10, fontWeight: 700,
                            background: `${sMeta.color}15`, color: sMeta.color,
                            border: `1px solid ${sMeta.color}25`,
                            display: 'inline-flex', alignItems: 'center', gap: 6,
                        }}>
                            <span style={{
                                width: 7,
                                height: 7,
                                borderRadius: '50%',
                                background: sMeta.color,
                                boxShadow: `0 0 8px ${sMeta.color}55`,
                            }} />
                            {lang === 'zh' ? sMeta.label_zh : sMeta.label_en}
                        </span>
                        {ne.assigned_model && (
                            <span style={{
                                padding: '2px 6px', borderRadius: 4, fontSize: 9,
                                background: 'var(--glass)', color: 'var(--text2)',
                            }}>
                                {ne.assigned_model}
                            </span>
                        )}
                        {ne.assigned_provider && (
                            <span style={{
                                padding: '2px 6px', borderRadius: 4, fontSize: 8,
                                background: 'var(--glass)', color: 'var(--text3)',
                            }}>
                                {ne.assigned_provider}
                            </span>
                        )}
                    </div>
                    <h3 style={{ fontSize: 14, fontWeight: 700, color: 'var(--text1)', lineHeight: 1.3 }}>
                        {ne.node_label || ne.node_key}
                    </h3>
                    <div style={{ fontSize: 8, color: 'var(--text3)', marginTop: 3 }}>
                        ID: {ne.id}
                    </div>
                </div>
                <button className="modal-close" onClick={onClose} style={{ marginTop: 2 }}>✕</button>
            </div>

            {/* Content */}
            <div style={{ flex: 1, overflow: 'auto', padding: 16 }}>
                {/* Metrics Grid */}
                <div style={{
                    display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8,
                    marginBottom: 14,
                }}>
                    {[
                        { label: tr('开始', 'Started'), value: formatTs(ne.started_at, lang) },
                        { label: tr('耗时', 'Duration'), value: durationStr(ne.started_at, ne.ended_at) },
                        { label: tr('重试', 'Retries'), value: String(ne.retry_count || 0) },
                        { label: 'Tokens', value: ne.tokens_used ? ne.tokens_used.toLocaleString() : '--' },
                        { label: tr('费用', 'Cost'), value: ne.cost ? `$${ne.cost.toFixed(3)}` : '--' },
                        { label: tr('产出物', 'Artifacts'), value: `${artifacts.length}` },
                    ].map((m, i) => (
                        <div key={i} style={{
                            padding: '8px 10px', borderRadius: 8,
                            background: 'var(--glass)', border: '1px solid var(--glass-border)',
                            textAlign: 'center',
                        }}>
                            <div style={{ fontSize: 8, color: 'var(--text3)', marginBottom: 2 }}>{m.label}</div>
                            <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text1)' }}>{m.value}</div>
                        </div>
                    ))}
                </div>

                {(ne.retried_from_id || ne.retry_count > 0) && (
                    <div style={{
                        marginBottom: 12,
                        padding: '8px 10px',
                        borderRadius: 8,
                        background: 'var(--glass)',
                        border: '1px solid var(--glass-border)',
                    }}>
                        {ne.retried_from_id && (
                            <div style={{ fontSize: 9, color: 'var(--text2)', marginBottom: 4 }}>
                                {tr('重试来源', 'Retried From')}: <span style={{ fontFamily: 'var(--font-mono), monospace' }}>{ne.retried_from_id}</span>
                            </div>
                        )}
                        <div style={{ fontSize: 9, color: retryLimitReached ? '#f59e0b' : 'var(--text3)' }}>
                            {tr('重试预算', 'Retry Budget')}: {retryBudgetRemaining}/{MAX_NODE_RETRY_COUNT} {tr('剩余', 'remaining')}
                        </div>
                    </div>
                )}

                {retryLimitReached && (
                    <div style={{
                        marginBottom: 12,
                        padding: '8px 10px',
                        borderRadius: 8,
                        background: 'rgba(245, 158, 11, 0.06)',
                        border: '1px solid rgba(245, 158, 11, 0.15)',
                        color: '#f59e0b',
                        fontSize: 10,
                        lineHeight: 1.5,
                    }}>
                        {tr('该节点已达到最大重试次数，需新建运行或手动介入', 'This node has reached the retry limit. Start a new run or intervene manually.')}
                    </div>
                )}

                {/* Input Summary */}
                {ne.input_summary && (
                    <div className="s-section" style={{ marginBottom: 12 }}>
                        <div className="s-section-title">{tr('输入摘要', 'Input Summary')}</div>
                        <div style={{
                            fontSize: 10, color: 'var(--text2)', lineHeight: 1.6,
                            background: 'var(--glass)', borderRadius: 8, padding: '8px 10px',
                            border: '1px solid var(--glass-border)',
                            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                            maxHeight: 150, overflow: 'auto',
                        }}>
                            {ne.input_summary}
                        </div>
                    </div>
                )}

                {/* Output Summary */}
                {ne.output_summary && (
                    <div className="s-section" style={{ marginBottom: 12 }}>
                        <div className="s-section-title">{tr('输出摘要', 'Output Summary')}</div>
                        <div style={{
                            fontSize: 10, color: 'var(--text2)', lineHeight: 1.7,
                            background: 'var(--glass)', borderRadius: 8, padding: '8px 10px',
                            border: '1px solid var(--glass-border)',
                            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                            maxHeight: 200, overflow: 'auto',
                        }}>
                            {readableOutputSummary}
                        </div>
                        {outputSections.length > 0 && (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
                                {outputSections.map((section) => (
                                    <div
                                        key={section.title}
                                        style={{
                                            borderRadius: 8,
                                            padding: '8px 10px',
                                            background: 'rgba(255,255,255,0.03)',
                                            border: '1px solid var(--glass-border)',
                                        }}
                                    >
                                        <div style={{ fontSize: 9, fontWeight: 700, color: 'var(--text1)', marginBottom: 4 }}>
                                            {section.title}
                                        </div>
                                        <div style={{
                                            fontSize: 9,
                                            color: 'var(--text2)',
                                            lineHeight: 1.6,
                                            whiteSpace: 'pre-wrap',
                                            wordBreak: 'break-word',
                                        }}>
                                            {section.text}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                )}

                {loadedSkills.length > 0 && (
                    <div className="s-section" style={{ marginBottom: 12 }}>
                        <div className="s-section-title">{tr('已加载技能', 'Loaded Skills')}</div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                            {loadedSkills.map((skill) => (
                                <span
                                    key={skill}
                                    style={{
                                        padding: '4px 8px',
                                        borderRadius: 999,
                                        fontSize: 9,
                                        color: '#e9d5ff',
                                        background: 'rgba(168,85,247,0.12)',
                                        border: '1px solid rgba(168,85,247,0.2)',
                                    }}
                                >
                                    {formatSkillLabel(skill)}
                                </span>
                            ))}
                        </div>
                    </div>
                )}

                {referenceUrls.length > 0 && (
                    <div className="s-section" style={{ marginBottom: 12 }}>
                        <div className="s-section-title">{tr('参考网址', 'Reference URLs')}</div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 160, overflow: 'auto' }}>
                            {referenceUrls.map((url) => (
                                <a
                                    key={url}
                                    href={url}
                                    target="_blank"
                                    rel="noreferrer"
                                    style={{
                                        fontSize: 9,
                                        color: '#93c5fd',
                                        lineHeight: 1.5,
                                        wordBreak: 'break-all',
                                        textDecoration: 'none',
                                        padding: '7px 9px',
                                        borderRadius: 8,
                                        background: 'var(--glass)',
                                        border: '1px solid var(--glass-border)',
                                    }}
                                >
                                    {url}
                                </a>
                            ))}
                        </div>
                    </div>
                )}

                {activityLog.length > 0 && (
                    <div className="s-section" style={{ marginBottom: 12 }}>
                        <div className="s-section-title">{tr('执行历史', 'Execution History')}</div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 220, overflow: 'auto' }}>
                            {activityLog.map((item, index) => (
                                <div
                                    key={`${item.ts}-${index}`}
                                    style={{
                                        borderRadius: 8,
                                        padding: '8px 10px',
                                        background: 'var(--glass)',
                                        border: '1px solid var(--glass-border)',
                                    }}
                                >
                                    <div style={{ fontSize: 8, color: 'var(--text3)', marginBottom: 4 }}>
                                        {formatTs(item.ts, lang)}
                                    </div>
                                    <div style={{ fontSize: 9, color: 'var(--text2)', lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                        {item.msg}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                {/* Error Message */}
                {ne.error_message && (
                    <div className="s-section" style={{ marginBottom: 12 }}>
                        <div className="s-section-title">{tr('错误信息', 'Error Message')}</div>
                        <div style={{
                            fontSize: 10, color: '#ef4444', lineHeight: 1.6,
                            background: 'rgba(239, 68, 68, 0.06)', borderRadius: 8,
                            padding: '8px 10px', border: '1px solid rgba(239, 68, 68, 0.15)',
                            fontFamily: 'var(--font-mono), monospace',
                            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                            maxHeight: 200, overflow: 'auto',
                        }}>
                            {ne.error_message}
                        </div>
                    </div>
                )}

                {/* Artifacts */}
                <div className="s-section">
                    <div className="s-section-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        {tr('产出物', 'Artifacts')}
                        {artLoading && <span style={{ fontSize: 8, color: 'var(--text3)' }}>({tr('加载中…', 'loading…')})</span>}
                    </div>

                    {artifacts.length === 0 && !artLoading ? (
                        <div style={{
                            padding: '12px', borderRadius: 8,
                            background: 'var(--glass)', border: '1px solid var(--glass-border)',
                            color: 'var(--text3)', fontSize: 10, textAlign: 'center',
                        }}>
                            {tr('暂无产出物', 'No artifacts yet')}
                        </div>
                    ) : (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                            {artifacts.map((art) => {
                                const aMeta = ARTIFACT_TYPE_META[art.artifact_type] || { mark: 'DOC', label_en: art.artifact_type, label_zh: art.artifact_type };
                                const isExpanded = expandedArt === art.id;

                                return (
                                    <div key={art.id} style={{
                                        borderRadius: 8,
                                        border: `1px solid ${isExpanded ? 'var(--accent)40' : 'var(--glass-border)'}`,
                                        background: isExpanded ? 'rgba(79, 143, 255, 0.03)' : 'var(--glass)',
                                        overflow: 'hidden',
                                        transition: 'all 0.15s',
                                    }}>
                                        {/* Artifact header */}
                                        <div
                                            onClick={() => setExpandedArt(isExpanded ? null : art.id)}
                                            style={{
                                                padding: '8px 10px', cursor: 'pointer',
                                                display: 'flex', alignItems: 'center', gap: 6,
                                            }}
                                        >
                                            <span style={{
                                                minWidth: 24,
                                                height: 18,
                                                borderRadius: 6,
                                                border: '1px solid var(--glass-border)',
                                                background: 'var(--glass)',
                                                display: 'inline-flex',
                                                alignItems: 'center',
                                                justifyContent: 'center',
                                                fontSize: 8,
                                                fontWeight: 700,
                                                color: 'var(--text2)',
                                                letterSpacing: '0.06em',
                                            }}>{aMeta.mark}</span>
                                            <div style={{ flex: 1, minWidth: 0 }}>
                                                <div style={{
                                                    fontSize: 10, fontWeight: 600, color: 'var(--text1)',
                                                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                                }}>
                                                    {art.title || (lang === 'zh' ? aMeta.label_zh : aMeta.label_en)}
                                                </div>
                                                <div style={{ fontSize: 8, color: 'var(--text3)' }}>
                                                    {lang === 'zh' ? aMeta.label_zh : aMeta.label_en}
                                                    {art.path && ` · ${art.path.split('/').pop()}`}
                                                </div>
                                            </div>
                                            <span style={{
                                                fontSize: 9, color: 'var(--text3)',
                                                transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
                                                transition: 'transform 0.15s',
                                            }}>▼</span>
                                        </div>

                                        {/* Expanded content */}
                                        {isExpanded && (
                                            <div style={{
                                                borderTop: '1px solid var(--glass-border)',
                                                padding: '8px 10px',
                                            }}>
                                                {art.path && (
                                                    <div style={{
                                                        fontSize: 9, color: 'var(--text2)', marginBottom: 6,
                                                        fontFamily: 'var(--font-mono), monospace',
                                                        wordBreak: 'break-all',
                                                    }}>
                                                        📁 {art.path}
                                                    </div>
                                                )}
                                                {art.content ? (
                                                    <div style={{
                                                        fontSize: 9, color: 'var(--text2)', lineHeight: 1.5,
                                                        background: 'rgba(0,0,0,0.15)', borderRadius: 6,
                                                        padding: '8px 10px', maxHeight: 300, overflow: 'auto',
                                                        fontFamily: 'var(--font-mono), monospace',
                                                        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                                                    }}>
                                                        {art.content}
                                                    </div>
                                                ) : (
                                                    <div style={{
                                                        fontSize: 9, color: 'var(--text3)', textAlign: 'center',
                                                        padding: 8,
                                                    }}>
                                                        {tr('内容为空或存储在文件路径中', 'Content empty or stored at file path')}
                                                    </div>
                                                )}
                                                {art.metadata && Object.keys(art.metadata).length > 0 && (
                                                    <div style={{
                                                        marginTop: 6, fontSize: 8, color: 'var(--text3)',
                                                    }}>
                                                        {Object.entries(art.metadata).map(([k, v]) => (
                                                            <span key={k} style={{
                                                                marginRight: 8, padding: '1px 4px',
                                                                borderRadius: 3, background: 'var(--glass)',
                                                            }}>
                                                                {k}: {String(v)}
                                                            </span>
                                                        ))}
                                                    </div>
                                                )}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>
            </div>

            {/* Action Footer */}
            <div style={{
                padding: '10px 16px',
                borderTop: '1px solid var(--glass-border)',
                display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap',
            }}>
                {retryError && (
                    <div style={{
                        width: '100%',
                        fontSize: 10,
                        color: '#ef4444',
                        background: 'rgba(239, 68, 68, 0.06)',
                        border: '1px solid rgba(239, 68, 68, 0.15)',
                        borderRadius: 6,
                        padding: '6px 8px',
                    }}>
                        {retryError}
                    </div>
                )}
                {(ne.status === 'failed' || ne.status === 'blocked') && (
                    <button
                        className="btn btn-primary"
                        disabled={retrying || retryLimitReached}
                        onClick={async () => {
                            try {
                                setRetrying(true);
                                setRetryError('');
                                const { nodeExecution: newNe } = await retryNodeExecution(ne.id);
                                onRetry?.(newNe);
                                // Switch to show the new node execution
                                currentNodeIdRef.current = newNe.id;
                                setNe(newNe);
                                setArtifacts([]);
                                setExpandedArt(null);
                                void refreshNode(newNe.id);
                                void fetchArtifacts(newNe.id);
                            } catch (e) {
                                console.error('[Inspector] Retry failed:', e);
                                setRetryError(e instanceof Error
                                    ? e.message
                                    : tr('节点重试失败，请稍后重试', 'Failed to retry node. Please try again.'));
                            } finally {
                                setRetrying(false);
                            }
                        }}
                        style={{ fontSize: 11 }}
                    >
                        🔄 {retrying
                            ? tr('重试中…', 'Retrying…')
                            : retryLimitReached
                                ? tr('已达重试上限', 'Retry Limit Reached')
                                : tr('重试节点', 'Retry Node')}
                    </button>
                )}
                <button
                    className="btn"
                    onClick={onClose}
                    style={{ fontSize: 11 }}
                >
                    {tr('关闭', 'Close')}
                </button>
            </div>
        </div>
    );
}
