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
        codeLines?: number;
        codeKb?: number;
        codeLanguages?: string[];
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
    const totalCodeLines = subtasks.reduce((sum, st) => sum + (st.codeLines || 0), 0);
    const totalCodeKb = subtasks.reduce((sum, st) => sum + (st.codeKb || 0), 0);
    const allLanguages = [...new Set(subtasks.flatMap(st => st.codeLanguages || []))];
    const passRate = total > 0 ? Math.round((completed / total) * 100) : 0;

    return (
        <div style={{
            borderRadius: 14,
            overflow: 'hidden',
            border: `1px solid ${success ? 'rgba(34,197,94,0.2)' : 'rgba(239,68,68,0.2)'}`,
            background: success
                ? 'linear-gradient(135deg, rgba(34,197,94,0.05), rgba(0,0,0,0.2))'
                : 'linear-gradient(135deg, rgba(239,68,68,0.05), rgba(0,0,0,0.2))',
        }}>
            {/* ── Header: Result ── */}
            <div style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '12px 14px',
                borderBottom: '1px solid rgba(255,255,255,0.06)',
                background: 'rgba(0,0,0,0.1)',
            }}>
                <span style={{
                    width: 10, height: 10, borderRadius: '50%',
                    background: dotColor,
                    boxShadow: `0 0 8px ${dotColor}80`,
                    flexShrink: 0,
                }} />
                <span style={{ fontSize: 13, fontWeight: 800, color: 'var(--text1)', letterSpacing: 0.2 }}>
                    {success ? t('Run Completed', '执行完成') : t('Run Failed', '执行失败')}
                </span>
                <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                    <span style={{
                        fontSize: 9, fontWeight: 700,
                        padding: '3px 8px', borderRadius: 999,
                        background: 'rgba(79,143,255,0.12)', color: '#60a5fa',
                        border: '1px solid rgba(79,143,255,0.2)',
                    }}>
                        {modeLabel}
                    </span>
                    <span style={{
                        fontSize: 9, fontWeight: 700,
                        padding: '3px 8px', borderRadius: 999,
                        background: success ? 'rgba(34,197,94,0.12)' : 'rgba(239,68,68,0.12)',
                        color: success ? '#4ade80' : '#f87171',
                        border: `1px solid ${success ? 'rgba(34,197,94,0.2)' : 'rgba(239,68,68,0.2)'}`,
                    }}>
                        {passRate}%
                    </span>
                </div>
            </div>

            {/* ── Stats Grid ── */}
            <div style={{
                display: 'grid', gridTemplateColumns: totalCodeLines > 0 ? 'repeat(4, 1fr)' : 'repeat(3, 1fr)',
                gap: 1, background: 'rgba(255,255,255,0.04)',
                borderBottom: '1px solid rgba(255,255,255,0.06)',
            }}>
                {[
                    { label: t('Nodes', '节点'), value: `${completed}/${total}`, color: '#60a5fa' },
                    { label: t('Retries', '重试'), value: String(retries), color: retries > 0 ? '#f59e0b' : '#6b7280' },
                    { label: t('Duration', '耗时'), value: formatDuration(durationSeconds, lang), color: '#c084fc' },
                    ...(totalCodeLines > 0 ? [{ label: t('Code', '代码'), value: `${totalCodeLines.toLocaleString()}L`, color: '#a78bfa' }] : []),
                ].map(item => (
                    <div key={item.label} style={{
                        textAlign: 'center', padding: '10px 4px',
                        background: 'rgba(0,0,0,0.15)',
                    }}>
                        <div style={{ fontSize: 9, color: 'var(--text3)', marginBottom: 3, fontWeight: 600 }}>{item.label}</div>
                        <div style={{ fontSize: 13, fontWeight: 800, color: item.color }}>{item.value}</div>
                    </div>
                ))}
            </div>

            {/* ── Code Output Metrics (v3.5) ── */}
            {totalCodeLines > 0 && (
                <div style={{
                    padding: '10px 14px',
                    borderBottom: '1px solid rgba(255,255,255,0.06)',
                    background: 'linear-gradient(135deg, rgba(167,139,250,0.06), transparent)',
                }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" strokeWidth="2">
                            <polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" />
                        </svg>
                        <span style={{ fontSize: 10, fontWeight: 700, color: '#a78bfa' }}>
                            {t('Code Output', '代码产出')}
                        </span>
                        <span style={{ fontSize: 10, color: 'var(--text3)', marginLeft: 'auto' }}>
                            {totalCodeKb > 0 ? `${Math.round(totalCodeKb)}KB` : ''}
                        </span>
                    </div>
                    {allLanguages.length > 0 && (
                        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                            {allLanguages.map(langTag => (
                                <span key={langTag} style={{
                                    padding: '2px 7px', borderRadius: 999, fontSize: 9, fontWeight: 600,
                                    background: 'rgba(167,139,250,0.1)',
                                    color: '#c4b5fd',
                                    border: '1px solid rgba(167,139,250,0.15)',
                                }}>{langTag}</span>
                            ))}
                        </div>
                    )}
                </div>
            )}

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

            {/* ── Pipeline Status (v3.5 antigravity-style) ── */}
            <div style={{
                padding: '10px 14px',
                borderBottom: '1px solid rgba(255,255,255,0.06)',
            }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text3)', marginBottom: 8, letterSpacing: 0.3 }}>
                    {t('PIPELINE STATUS', '流水线状态')}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    {subtasks.map(st => {
                        const isOk = isSuccessfulStatus(st.status);
                        const statusColor = isOk ? '#22c55e' : '#ef4444';
                        const agentColors: Record<string, string> = {
                            planner: '#60a5fa', analyst: '#f59e0b', builder: '#a78bfa',
                            merger: '#ec4899', reviewer: '#06b6d4', tester: '#22c55e',
                            deployer: '#f97316', debugger: '#ef4444', scribe: '#8b5cf6',
                            uidesign: '#fb7185', imagegen: '#eab308', polisher: '#c084fc',
                        };
                        const agentColor = agentColors[st.agent] || '#6b7280';
                        return (
                            <div key={st.id} style={{
                                display: 'flex', alignItems: 'center', gap: 8,
                                padding: '5px 8px', borderRadius: 8,
                                background: 'rgba(255,255,255,0.02)',
                                border: '1px solid rgba(255,255,255,0.04)',
                            }}>
                                <span style={{
                                    width: 6, height: 6, borderRadius: '50%',
                                    background: statusColor,
                                    boxShadow: `0 0 4px ${statusColor}60`,
                                    flexShrink: 0,
                                }} />
                                <span style={{
                                    fontSize: 10, fontWeight: 700,
                                    color: agentColor, minWidth: 65,
                                }}>
                                    {st.agent.charAt(0).toUpperCase() + st.agent.slice(1)}
                                </span>
                                <span style={{
                                    fontSize: 8, fontWeight: 700,
                                    padding: '1px 6px', borderRadius: 999,
                                    background: isOk ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)',
                                    color: isOk ? '#4ade80' : '#f87171',
                                    border: `1px solid ${isOk ? 'rgba(34,197,94,0.2)' : 'rgba(239,68,68,0.2)'}`,
                                }}>
                                    {isOk ? t('PASS', '通过') : t('FAIL', '失败')}
                                </span>
                                {(st.codeLines || 0) > 0 && (
                                    <span style={{
                                        fontSize: 8, color: '#a78bfa', fontWeight: 600,
                                        marginLeft: 'auto',
                                    }}>
                                        {(st.codeLines || 0).toLocaleString()}L
                                    </span>
                                )}
                                {st.retries > 0 && (
                                    <span style={{
                                        fontSize: 8, fontWeight: 600,
                                        padding: '1px 5px', borderRadius: 999,
                                        background: 'rgba(245,158,11,0.1)', color: '#fbbf24',
                                    }}>
                                        x{st.retries}
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
