'use client';

import { useCallback, useEffect, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';

/**
 * v7.0 (maintainer 2026-04-24) — Reports side panel (VSCode activity bar).
 * Lists recent pipeline runs; click to open full detail via the
 * existing Reports modal (onOpenFullReports callback).
 */

interface Report {
    id?: string;
    run_id?: string;
    goal?: string;
    difficulty?: string;
    duration_seconds?: number;
    completed?: number;
    failed?: number;
    pending?: number;
    total_subtasks?: number;
    success?: boolean;
    created_at?: number;
    summary?: string;
}

interface Props {
    lang: 'en' | 'zh';
    onOpenFullReports?: () => void;
    onCountChange?: (n: number) => void;
}

export default function ReportsPanel({ lang, onOpenFullReports, onCountChange }: Props) {
    const [reports, setReports] = useState<Report[]>([]);
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState('');

    const fetchReports = useCallback(async () => {
        try {
            setLoading(true);
            const r = await fetch(`${API_BASE}/api/reports`, { cache: 'no-store' });
            const j = await r.json();
            const list = (j.reports || []) as Report[];
            setReports(list);
            setErr('');
            if (onCountChange) onCountChange(list.length);
        } catch (e: unknown) {
            setErr(e instanceof Error ? e.message : String(e));
        } finally {
            setLoading(false);
        }
    }, [onCountChange]);

    useEffect(() => {
        fetchReports();
        const t = setInterval(fetchReports, 10_000);
        return () => clearInterval(t);
    }, [fetchReports]);

    const fmtDur = (s?: number) => {
        if (!s || s <= 0) return '—';
        const m = Math.floor(s / 60);
        const sec = Math.floor(s % 60);
        return `${m}m${sec.toString().padStart(2, '0')}s`;
    };

    const fmtTime = (ts?: number) => {
        if (!ts) return '';
        const d = new Date(ts * 1000);
        return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`;
    };

    const deleteOne = useCallback(async (id: string, e: React.MouseEvent) => {
        e.stopPropagation();
        if (!confirm(lang === 'zh' ? '删除这条报告？' : 'Delete this report?')) return;
        try {
            await fetch(`${API_BASE}/api/reports/${encodeURIComponent(id)}`, { method: 'DELETE' });
            await fetchReports();
        } catch {
            /* ignore */
        }
    }, [lang, fetchReports]);

    const clearAll = useCallback(async () => {
        const msg = lang === 'zh'
            ? `确定删除全部 ${reports.length} 条报告？此操作不可撤销。\n\n(如需保留最近 10 条，点"取消"后改用顶部按钮)`
            : `Delete all ${reports.length} reports? This cannot be undone.`;
        if (!confirm(msg)) return;
        try {
            await fetch(`${API_BASE}/api/reports`, { method: 'DELETE' });
            await fetchReports();
        } catch { /* ignore */ }
    }, [lang, reports.length, fetchReports]);

    const keepLatest10 = useCallback(async () => {
        if (reports.length <= 10) return;
        const msg = lang === 'zh'
            ? `只保留最近 10 条，删除其余 ${reports.length - 10} 条？`
            : `Keep latest 10, delete ${reports.length - 10} older?`;
        if (!confirm(msg)) return;
        try {
            await fetch(`${API_BASE}/api/reports?keep_latest=10`, { method: 'DELETE' });
            await fetchReports();
        } catch { /* ignore */ }
    }, [lang, reports.length, fetchReports]);

    return (
        <div className="flex flex-col h-full text-[11px]">
            <div className="px-3 py-2 border-b border-white/5 flex items-center justify-between">
                <div className="font-bold tracking-wider uppercase text-[var(--text2)]">
                    {lang === 'zh' ? '执行报告' : 'Reports'}
                </div>
                <div className="flex gap-1">
                    <button
                        onClick={fetchReports}
                        title={lang === 'zh' ? '刷新' : 'Refresh'}
                        className="px-1.5 py-0.5 rounded hover:bg-white/10 text-[var(--text3)] hover:text-[var(--text1)]"
                    >↻</button>
                    {reports.length > 10 && (
                        <button
                            onClick={keepLatest10}
                            title={lang === 'zh' ? `保留最近 10 条，删除 ${reports.length - 10} 条` : `Keep latest 10, delete ${reports.length - 10} older`}
                            className="px-1.5 py-0.5 rounded hover:bg-white/10 text-[var(--text3)] hover:text-[var(--text1)]"
                            style={{ fontSize: 10 }}
                        >10↓</button>
                    )}
                    {reports.length > 0 && (
                        <button
                            onClick={clearAll}
                            title={lang === 'zh' ? '清空全部报告' : 'Clear all'}
                            className="px-1.5 py-0.5 rounded hover:bg-red-500/20 text-[var(--text3)] hover:text-red-400"
                            style={{ fontSize: 11 }}
                        >🗑</button>
                    )}
                    {onOpenFullReports && (
                        <button
                            onClick={onOpenFullReports}
                            title={lang === 'zh' ? '查看详情面板' : 'Open full reports'}
                            className="px-1.5 py-0.5 rounded hover:bg-white/10 text-[var(--text3)] hover:text-[var(--text1)]"
                        >⤢</button>
                    )}
                </div>
            </div>

            {err && (
                <div className="px-3 py-2 text-[10px] text-red-400">
                    {lang === 'zh' ? '加载失败: ' : 'Fetch failed: '}{err}
                </div>
            )}

            <div className="flex-1 overflow-y-auto">
                {reports.length === 0 && !loading && (
                    <div className="px-3 py-6 text-center text-[10px] text-[var(--text3)]">
                        {lang === 'zh' ? '尚无执行记录' : 'No runs yet'}
                    </div>
                )}
                {reports.slice(0, 30).map((r, i) => {
                    const success = (r.failed || 0) === 0 && (r.pending || 0) === 0 && (r.completed || 0) > 0;
                    return (
                        <div
                            key={r.id || r.run_id || i}
                            className="px-3 py-2 border-b border-white/5 hover:bg-white/5 cursor-pointer transition-colors group relative"
                            onClick={onOpenFullReports}
                            title={r.run_id}
                        >
                            <div className="flex items-center gap-2 mb-1">
                                <span style={{
                                    width: 8, height: 8, borderRadius: '50%',
                                    background: success ? '#3fb950' : (r.failed ? '#f85149' : '#d29922'),
                                    flexShrink: 0,
                                }} />
                                <span className="flex-1 truncate font-semibold text-[10px]" title={r.goal}>
                                    {(r.goal || r.run_id || 'untitled').slice(0, 40)}
                                </span>
                                <span className="text-[8px] text-[var(--text3)]">{fmtTime(r.created_at)}</span>
                                {(r.id || r.run_id) && (
                                    <button
                                        onClick={(e) => deleteOne(String(r.id || r.run_id || ''), e)}
                                        title={lang === 'zh' ? '删除' : 'Delete'}
                                        className="opacity-0 group-hover:opacity-100 transition-opacity ml-1 text-[var(--text3)] hover:text-red-400"
                                        style={{ fontSize: 11, padding: '0 3px' }}
                                    >×</button>
                                )}
                            </div>
                            <div className="flex items-center gap-2 text-[9px] text-[var(--text3)] pl-4">
                                <span>{fmtDur(r.duration_seconds)}</span>
                                <span>·</span>
                                <span style={{ color: '#3fb950' }}>{r.completed || 0}✓</span>
                                {(r.failed || 0) > 0 && <span style={{ color: '#f85149' }}>{r.failed}✗</span>}
                                <span>/{r.total_subtasks || '?'}</span>
                                <span className="ml-auto text-[8px] uppercase" style={{ letterSpacing: 0.5 }}>{r.difficulty || ''}</span>
                            </div>
                        </div>
                    );
                })}
            </div>

            <div className="px-3 py-1.5 border-t border-white/5 text-[9px] text-[var(--text3)]">
                {lang === 'zh' ? `共 ${reports.length} 次执行` : `${reports.length} runs`}
                {loading && <span className="ml-2">…</span>}
            </div>
        </div>
    );
}
