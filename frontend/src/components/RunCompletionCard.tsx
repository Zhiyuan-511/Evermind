'use client';

/**
 * RunCompletionCard — P2-3 Result Summary
 *
 * Renders a structured, human-readable summary when a workflow run completes.
 * Replaces the raw inline HTML dump with clear sections:
 *  1. Result status (pass / fail dot)
 *  2. Stats grid (nodes, retries, duration, mode)
 *  3. Files changed (if available)
 *  4. Validation summary (if reviewer/tester output present)
 *  5. Preview link (if available)
 *  6. Next actions guidance
 */

interface RunCompletionCardProps {
    success: boolean;
    completed: number;
    total: number;
    retries: number;
    durationSeconds: number;
    difficulty: string;
    subtasks: Array<{
        id: string;
        agent: string;
        status: string;
        retries: number;
        filesCreated?: string[];
        workSummary?: string[];
    }>;
    previewUrl?: string;
    lang: 'en' | 'zh';
    onOpenReports?: () => void;
    onRevealInFinder?: () => void;
}

function formatDuration(sec: number, lang: 'en' | 'zh'): string {
    const rounded = Math.round(sec);
    if (rounded < 60) return lang === 'zh' ? `${rounded} 秒` : `${rounded}s`;
    const m = Math.floor(rounded / 60);
    const s = rounded % 60;
    return lang === 'zh' ? `${m} 分 ${s} 秒` : `${m}m ${s}s`;
}

const DIFF_LABEL: Record<string, Record<string, string>> = {
    simple:   { en: 'Blitz',    zh: '极速' },
    standard: { en: 'Balanced', zh: '平衡' },
    pro:      { en: 'Deep',     zh: '深度' },
};

export default function RunCompletionCard({
    success, completed, total, retries, durationSeconds, difficulty,
    subtasks, previewUrl, lang, onOpenReports, onRevealInFinder,
}: RunCompletionCardProps) {
    const t = (en: string, zh: string) => lang === 'zh' ? zh : en;
    const isSuccessfulStatus = (status: string) => ['completed', 'passed', 'done', 'success'].includes(String(status || '').trim().toLowerCase());

    // Collect files
    const allFiles = subtasks.flatMap(st => st.filesCreated || []);
    const uniqueFiles = [...new Set(allFiles)];

    // Collect work summaries
    const summaryItems = subtasks.flatMap(st => st.workSummary || []).slice(0, 6);

    const modeLabel = DIFF_LABEL[difficulty]?.[lang] || difficulty;
    const dotColor = success ? '#22c55e' : '#ef4444';

    return (
        <div style={{
            borderRadius: 10,
            overflow: 'hidden',
            border: `1px solid ${success ? 'rgba(34,197,94,0.2)' : 'rgba(239,68,68,0.2)'}`,
            background: success ? 'rgba(34,197,94,0.03)' : 'rgba(239,68,68,0.03)',
        }}>
            {/* ── Header: Result ── */}
            <div style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '10px 12px',
                borderBottom: '1px solid rgba(255,255,255,0.06)',
            }}>
                <span style={{
                    width: 8, height: 8, borderRadius: '50%',
                    background: dotColor,
                    boxShadow: `0 0 6px ${dotColor}`,
                    flexShrink: 0,
                }} />
                <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--text1)' }}>
                    {success ? t('Run Completed', '执行完成') : t('Run Failed', '执行失败')}
                </span>
                <span style={{
                    fontSize: 9, fontWeight: 600,
                    marginLeft: 'auto',
                    padding: '2px 6px', borderRadius: 999,
                    background: 'rgba(79,143,255,0.1)', color: 'var(--blue)',
                }}>
                    {modeLabel}
                </span>
            </div>

            {/* ── Stats Grid ── */}
            <div style={{
                display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)',
                gap: 1, background: 'rgba(255,255,255,0.04)',
                borderBottom: '1px solid rgba(255,255,255,0.06)',
            }}>
                {[
                    { label: t('Nodes', '节点'), value: `${completed}/${total}` },
                    { label: t('Retries', '重试'), value: String(retries) },
                    { label: t('Duration', '耗时'), value: formatDuration(durationSeconds, lang) },
                ].map(item => (
                    <div key={item.label} style={{
                        textAlign: 'center', padding: '8px 4px',
                        background: 'rgba(0,0,0,0.15)',
                    }}>
                        <div style={{ fontSize: 8, color: 'var(--text3)', marginBottom: 2 }}>{item.label}</div>
                        <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text1)' }}>{item.value}</div>
                    </div>
                ))}
            </div>

            {/* ── Work Summary ── */}
            {summaryItems.length > 0 && (
                <div style={{
                    padding: '8px 12px',
                    borderBottom: '1px solid rgba(255,255,255,0.06)',
                }}>
                    <div style={{ fontSize: 9, fontWeight: 600, color: 'var(--text3)', marginBottom: 4 }}>
                        {t('What was done', '完成内容')}
                    </div>
                    {summaryItems.map((item, idx) => (
                        <div key={`s-${idx}`} style={{ fontSize: 10, color: 'var(--text2)', lineHeight: 1.5 }}>
                            • {item}
                        </div>
                    ))}
                </div>
            )}

            {/* ── Files Changed ── */}
            {uniqueFiles.length > 0 && (
                <div style={{
                    padding: '8px 12px',
                    borderBottom: '1px solid rgba(255,255,255,0.06)',
                }}>
                    <div style={{ fontSize: 9, fontWeight: 600, color: 'var(--text3)', marginBottom: 4 }}>
                        {t('Files Created', '创建的文件')}
                    </div>
                    {uniqueFiles.slice(0, 8).map((file, idx) => (
                        <div key={`f-${idx}`} style={{
                            fontSize: 10, color: 'var(--text2)', lineHeight: 1.5,
                            fontFamily: 'monospace',
                        }}>
                            {file}
                        </div>
                    ))}
                    {uniqueFiles.length > 8 && (
                        <div style={{ fontSize: 9, color: 'var(--text3)' }}>
                            + {uniqueFiles.length - 8} {t('more', '更多')}
                        </div>
                    )}
                </div>
            )}

            {/* ── Validation Status ── */}
            <div style={{
                padding: '8px 12px',
                borderBottom: previewUrl ? '1px solid rgba(255,255,255,0.06)' : 'none',
            }}>
                <div style={{ fontSize: 9, fontWeight: 600, color: 'var(--text3)', marginBottom: 4 }}>
                    {t('Validation', '验证状态')}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                    {subtasks.map(st => {
                        const isOk = isSuccessfulStatus(st.status);
                        return (
                            <div key={st.id} style={{
                                fontSize: 10, color: 'var(--text2)',
                                display: 'flex', alignItems: 'center', gap: 6,
                            }}>
                                <span style={{
                                    width: 5, height: 5, borderRadius: '50%',
                                    background: isOk ? '#22c55e' : '#ef4444',
                                    flexShrink: 0,
                                }} />
                                <span style={{ fontWeight: 600 }}>
                                    {st.agent.charAt(0).toUpperCase() + st.agent.slice(1)}
                                </span>
                                <span style={{ color: isOk ? 'var(--green)' : 'var(--red)', fontSize: 9 }}>
                                    {isOk ? t('passed', '通过') : t('failed', '失败')}
                                </span>
                                {st.retries > 0 && (
                                    <span style={{ fontSize: 8, color: 'var(--text3)' }}>
                                        ({st.retries} {t('retries', '次重试')})
                                    </span>
                                )}
                            </div>
                        );
                    })}
                </div>
            </div>

            {/* ── §3.6: Deliverable Status — separate workflow-done from result-ready ── */}
            <div style={{
                padding: '8px 12px',
                borderBottom: '1px solid rgba(255,255,255,0.06)',
            }}>
                <div style={{ fontSize: 9, fontWeight: 600, color: 'var(--text3)', marginBottom: 6 }}>
                    {t('Completion Status', '完成状态')}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                    {/* Row 1: Workflow status */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10 }}>
                        <span style={{
                            width: 14, height: 14, borderRadius: '50%',
                            background: success ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)',
                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                            fontSize: 9, flexShrink: 0,
                        }}>
                            {success ? '✓' : '✗'}
                        </span>
                        <span style={{ fontWeight: 600, color: 'var(--text1)' }}>
                            {t('Workflow', '流程执行')}
                        </span>
                        <span style={{
                            fontSize: 9, fontWeight: 600, marginLeft: 'auto',
                            color: success ? '#22c55e' : '#ef4444',
                        }}>
                            {success ? t('Completed', '已完成') : t('Failed', '失败')}
                        </span>
                    </div>
                    {/* Row 2: Deliverable / Preview status */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10 }}>
                        <span style={{
                            width: 14, height: 14, borderRadius: '50%',
                            background: previewUrl ? 'rgba(34,197,94,0.15)' : 'rgba(245,158,11,0.15)',
                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                            fontSize: 9, flexShrink: 0,
                        }}>
                            {previewUrl ? '✓' : '⏳'}
                        </span>
                        <span style={{ fontWeight: 600, color: 'var(--text1)' }}>
                            {t('Deliverable', '结果产物')}
                        </span>
                        <span style={{
                            fontSize: 9, fontWeight: 600, marginLeft: 'auto',
                            color: previewUrl ? '#22c55e' : '#f59e0b',
                        }}>
                            {previewUrl
                                ? t('Ready', '就绪')
                                : (uniqueFiles.length > 0
                                    ? t('Files only', '仅文件')
                                    : t('Not ready', '未就绪'))}
                        </span>
                    </div>
                </div>
                {/* Explanatory note when deliverables are not ready */}
                {!previewUrl && success && (
                    <div style={{
                        fontSize: 9, color: 'var(--text3)', marginTop: 6,
                        padding: '4px 8px', borderRadius: 4,
                        background: 'rgba(245,158,11,0.06)',
                        border: '1px solid rgba(245,158,11,0.1)',
                        lineHeight: 1.5,
                    }}>
                        {uniqueFiles.length > 0
                            ? t(
                                'The workflow completed and generated files, but no preview artifact is available for this task.',
                                '流程已成功完成，并已生成文件产物，但当前任务没有可预览结果。'
                            )
                            : t(
                                'The workflow completed successfully, but no preview artifact was generated. The deliverable may still be in progress or was not configured for this task.',
                                '流程已成功完成，但暂无预览产物。结果可能仍在生成中，或此任务未配置预览输出。'
                            )}
                    </div>
                )}
            </div>

            {/* ── Action Buttons (§3.4 强 CTA) ── */}
            <div style={{ padding: '10px 12px', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {previewUrl && (
                    <button
                        onClick={() => window.open(previewUrl, '_blank', 'noopener,noreferrer')}
                        style={{
                            flex: 2, minWidth: 120, padding: '10px 16px', fontSize: 12, fontWeight: 700,
                            borderRadius: 8, cursor: 'pointer', border: 'none',
                            background: 'linear-gradient(135deg, #3b82f6, #2563eb)',
                            color: '#fff', letterSpacing: 0.3,
                            boxShadow: '0 2px 8px rgba(59,130,246,0.3)',
                            transition: 'all 0.2s',
                        }}
                        onMouseEnter={e => {
                            e.currentTarget.style.transform = 'translateY(-1px)';
                            e.currentTarget.style.boxShadow = '0 4px 14px rgba(59,130,246,0.45)';
                        }}
                        onMouseLeave={e => {
                            e.currentTarget.style.transform = 'translateY(0)';
                            e.currentTarget.style.boxShadow = '0 2px 8px rgba(59,130,246,0.3)';
                        }}
                    >
                        🌐 {t('Open Preview', '打开预览')}
                    </button>
                )}
                {onOpenReports && (
                    <button
                        onClick={onOpenReports}
                        style={{
                            flex: 1, minWidth: 80, padding: '8px 12px', fontSize: 10, fontWeight: 600,
                            borderRadius: 6, cursor: 'pointer', border: 'none',
                            background: 'rgba(168,85,247,0.15)', color: '#a855f7',
                            transition: 'background 0.15s',
                        }}
                        onMouseEnter={e => (e.currentTarget.style.background = 'rgba(168,85,247,0.25)')}
                        onMouseLeave={e => (e.currentTarget.style.background = 'rgba(168,85,247,0.15)')}
                    >
                        📋 {t('View Reports', '查看报告')}
                    </button>
                )}
                {onRevealInFinder && (
                    <button
                        onClick={onRevealInFinder}
                        style={{
                            flex: 1, minWidth: 80, padding: '8px 12px', fontSize: 10, fontWeight: 600,
                            borderRadius: 6, cursor: 'pointer',
                            background: 'rgba(255,255,255,0.06)', color: 'var(--text2)',
                            border: '1px solid var(--glass-border)',
                            transition: 'background 0.15s',
                        }}
                        onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.12)')}
                        onMouseLeave={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.06)')}
                    >
                        📂 {t('Reveal in Finder', '在 Finder 中显示')}
                    </button>
                )}
            </div>
        </div>
    );
}
