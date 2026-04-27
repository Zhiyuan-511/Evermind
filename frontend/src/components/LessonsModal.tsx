/**
 * v5.5 Compound Engineering UI — displays the lessons Evermind has learned
 * across pipeline runs. Lets the user see "how smart I've gotten" per task type.
 *
 * Backend APIs consumed:
 *   GET /api/compound/stats  -> { total, per_task_type, file }
 *   GET /api/compound/lessons?task_type=<t>&limit=50
 */
import React, { useEffect, useMemo, useState } from 'react';

type Lesson = {
    ts?: number;
    task_type?: string;
    task_hint?: string;
    lesson?: string;
    source?: string;
    severity?: string;
    hash?: string;
};

type StatsResponse = {
    ok: boolean;
    total?: number;
    per_task_type?: Record<string, number>;
    file?: string;
    error?: string;
};

type LessonsResponse = {
    ok: boolean;
    items?: Lesson[];
    total_stored?: number;
    error?: string;
};

// v5.5: severity markers — color-coded pill badges, no emoji.
const severityPalette: Record<string, { bg: string; border: string; color: string; dot: string }> = {
    blocking: { bg: 'rgba(239,68,68,0.10)', border: 'rgba(239,68,68,0.30)', color: '#fca5a5', dot: '#ef4444' },
    warning:  { bg: 'rgba(245,158,11,0.10)', border: 'rgba(245,158,11,0.30)', color: '#fcd34d', dot: '#f59e0b' },
    info:     { bg: 'rgba(59,130,246,0.08)', border: 'rgba(59,130,246,0.25)', color: '#93c5fd', dot: '#3b82f6' },
};

function formatRelativeTime(ts?: number, lang: 'zh' | 'en' = 'zh'): string {
    if (!ts) return '';
    const deltaSec = Math.max(0, Date.now() / 1000 - ts);
    if (deltaSec < 60) return lang === 'zh' ? '刚刚' : 'just now';
    if (deltaSec < 3600) return lang === 'zh' ? `${Math.floor(deltaSec / 60)} 分钟前` : `${Math.floor(deltaSec / 60)}m ago`;
    if (deltaSec < 86400) return lang === 'zh' ? `${Math.floor(deltaSec / 3600)} 小时前` : `${Math.floor(deltaSec / 3600)}h ago`;
    return lang === 'zh' ? `${Math.floor(deltaSec / 86400)} 天前` : `${Math.floor(deltaSec / 86400)}d ago`;
}

export default function LessonsModal({
    open,
    onClose,
    lang = 'zh',
}: {
    open: boolean;
    onClose: () => void;
    lang?: 'zh' | 'en';
}) {
    const [stats, setStats] = useState<StatsResponse | null>(null);
    const [taskType, setTaskType] = useState<string>('');
    const [lessons, setLessons] = useState<Lesson[]>([]);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        if (!open) return;
        let cancelled = false;
        (async () => {
            try {
                const r = await fetch('/api/compound/stats');
                const d: StatsResponse = await r.json();
                if (!cancelled) setStats(d);
                const firstType = Object.keys(d?.per_task_type || {})[0] || '';
                if (firstType && !taskType) setTaskType(firstType);
            } catch (e) {
                if (!cancelled) setStats({ ok: false, error: String(e) });
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [open]);

    useEffect(() => {
        if (!open || !taskType) return;
        let cancelled = false;
        setLoading(true);
        (async () => {
            try {
                const r = await fetch(`/api/compound/lessons?task_type=${encodeURIComponent(taskType)}&limit=50`);
                const d: LessonsResponse = await r.json();
                if (!cancelled) setLessons(d.items || []);
            } catch {
                if (!cancelled) setLessons([]);
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [open, taskType]);

    const taskTypes = useMemo(() => Object.keys(stats?.per_task_type || {}).sort(), [stats]);

    if (!open) return null;

    const t = (zh: string, en: string) => (lang === 'en' ? en : zh);

    return (
        <div
            style={{
                position: 'fixed', inset: 0, zIndex: 2000,
                background: 'rgba(2,6,23,0.75)', backdropFilter: 'blur(6px)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                padding: 24,
            }}
            onClick={onClose}
        >
            <div
                style={{
                    width: 'min(960px, 100%)', maxHeight: '88vh',
                    background: 'linear-gradient(180deg, rgba(15,23,42,0.98), rgba(2,6,23,0.98))',
                    border: '1px solid rgba(255,255,255,0.08)', borderRadius: 18,
                    boxShadow: '0 24px 72px rgba(0,0,0,0.55), inset 0 1px 0 rgba(255,255,255,0.06)',
                    display: 'flex', flexDirection: 'column', overflow: 'hidden',
                }}
                onClick={e => e.stopPropagation()}
            >
                {/* Header */}
                <div style={{
                    padding: '18px 24px', borderBottom: '1px solid rgba(255,255,255,0.06)',
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#7dd3fc" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                            <path d="M12 3a6 6 0 0 0-3.5 10.9V16a1.5 1.5 0 0 0 1.5 1.5h4A1.5 1.5 0 0 0 15.5 16v-2.1A6 6 0 0 0 12 3Z"/>
                            <path d="M9 20h6M10 22h4M12 7v4M10 9h4"/>
                        </svg>
                        <div>
                            <div style={{ fontSize: 18, fontWeight: 700, color: '#f8fafc', letterSpacing: 0.2 }}>
                                {t('复合工程 · 已学到的经验', 'Compound Engineering · Lessons Learned')}
                            </div>
                            <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>
                                {t(
                                    'Evermind 从过往每一轮任务中自动提炼的经验,下一次同类任务会自动规避这些问题',
                                    'Distilled automatically from every past run — applied to future runs of the same task type.',
                                )}
                            </div>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        style={{
                            background: 'transparent', border: '1px solid rgba(255,255,255,0.12)',
                            color: '#cbd5e1', padding: '6px 12px', borderRadius: 8, cursor: 'pointer',
                            fontSize: 12,
                        }}
                    >
                        {t('关闭', 'Close')}
                    </button>
                </div>

                {/* Stats band */}
                <div style={{
                    padding: '14px 24px', background: 'rgba(255,255,255,0.02)',
                    borderBottom: '1px solid rgba(255,255,255,0.04)',
                    display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'center',
                }}>
                    <div>
                        <div style={{ fontSize: 11, color: '#64748b', textTransform: 'uppercase', letterSpacing: 0.5 }}>
                            {t('累计教训', 'Total lessons')}
                        </div>
                        <div style={{ fontSize: 24, fontWeight: 700, color: '#f8fafc' }}>
                            {stats?.total ?? '—'}
                        </div>
                    </div>
                    <div style={{ flex: 1, minWidth: 260 }}>
                        <div style={{ fontSize: 11, color: '#64748b', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>
                            {t('按任务类型', 'Per task type')}
                        </div>
                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                            {taskTypes.length === 0 && (
                                <span style={{ color: '#64748b', fontSize: 12, fontStyle: 'italic' }}>
                                    {t('暂无数据 — 跑一轮任务后回来看看', 'No data yet — run a pipeline, then come back.')}
                                </span>
                            )}
                            {taskTypes.map(tt => {
                                const active = tt === taskType;
                                return (
                                    <button
                                        key={tt}
                                        onClick={() => setTaskType(tt)}
                                        style={{
                                            padding: '4px 10px', borderRadius: 999,
                                            border: active
                                                ? '1px solid rgba(56,189,248,0.45)'
                                                : '1px solid rgba(255,255,255,0.08)',
                                            background: active ? 'rgba(56,189,248,0.18)' : 'rgba(255,255,255,0.03)',
                                            color: active ? '#7dd3fc' : '#cbd5e1',
                                            fontSize: 11, cursor: 'pointer',
                                        }}
                                    >
                                        {tt} · {stats?.per_task_type?.[tt] ?? 0}
                                    </button>
                                );
                            })}
                        </div>
                    </div>
                </div>

                {/* Lessons list */}
                <div style={{ flex: 1, overflowY: 'auto', padding: '16px 24px 24px' }}>
                    {loading && (
                        <div style={{ color: '#64748b', fontSize: 12, fontStyle: 'italic' }}>
                            {t('正在加载教训…', 'Loading lessons…')}
                        </div>
                    )}
                    {!loading && lessons.length === 0 && (
                        <div style={{ color: '#64748b', fontSize: 12, fontStyle: 'italic' }}>
                            {t('这个任务类型还没有累积教训。', 'No lessons stored for this task type yet.')}
                        </div>
                    )}
                    {!loading && lessons.map((l, i) => {
                        const sev = (l.severity || 'info').toLowerCase();
                        const pal = severityPalette[sev] || severityPalette.info;
                        return (
                            <div
                                key={l.hash || i}
                                style={{
                                    padding: '10px 14px', marginBottom: 8, borderRadius: 10,
                                    background: pal.bg, border: `1px solid ${pal.border}`,
                                    display: 'flex', gap: 10, alignItems: 'flex-start',
                                }}
                            >
                                <span
                                    style={{
                                        width: 8, height: 8, borderRadius: '50%', background: pal.dot,
                                        boxShadow: `0 0 6px ${pal.dot}88`, marginTop: 6, flexShrink: 0,
                                    }}
                                    aria-hidden
                                />
                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <div style={{ color: '#e2e8f0', fontSize: 13, lineHeight: 1.5 }}>
                                        {l.lesson}
                                    </div>
                                    <div style={{
                                        marginTop: 6, display: 'flex', gap: 10, flexWrap: 'wrap',
                                        fontSize: 10, color: '#64748b',
                                    }}>
                                        <span style={{ color: pal.color, fontWeight: 600 }}>{sev.toUpperCase()}</span>
                                        {l.source && <span>📍 {l.source}</span>}
                                        {l.task_hint && (
                                            <span title={l.task_hint} style={{
                                                maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                            }}>
                                                {t('来源:', 'from:')} {l.task_hint}
                                            </span>
                                        )}
                                        <span style={{ marginLeft: 'auto', fontStyle: 'italic' }}>
                                            {formatRelativeTime(l.ts, lang)}
                                        </span>
                                    </div>
                                </div>
                            </div>
                        );
                    })}
                </div>

                {stats?.file && (
                    <div style={{
                        padding: '8px 24px', borderTop: '1px solid rgba(255,255,255,0.04)',
                        fontSize: 10, color: '#475569', fontFamily: 'ui-monospace, monospace',
                    }}>
                        {t('存储于:', 'Stored at:')} {stats.file}
                    </div>
                )}
            </div>
        </div>
    );
}
