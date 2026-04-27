'use client';

import { useCallback, useEffect, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';

/**
 * v7.0 (maintainer 2026-04-24) — Source Control panel.
 * Shows per-file git diff with red (deleted) / green (added) hunks,
 * Cursor/VSCode style. Auto-refreshes every 5s while the panel is open
 * so Chat Agent edits surface live.
 */

interface Hunk {
    type: 'add' | 'del' | 'ctx' | 'hunk_header';
    content: string;
}

interface GitFile {
    path: string;
    status: 'modified' | 'added' | 'deleted' | 'renamed' | 'untracked';
    hunks: Hunk[];
}

interface DiffResponse {
    is_repo: boolean;
    project_path?: string;
    files: GitFile[];
    summary?: { total_files: number; added_lines: number; deleted_lines: number };
}

interface Props {
    lang: 'en' | 'zh';
    onDirtyCountChange?: (n: number) => void;
}

const STATUS_COLORS: Record<string, string> = {
    modified: '#d29922',   // amber
    added: '#3fb950',      // green
    deleted: '#f85149',    // red
    renamed: '#58a6ff',    // blue
    untracked: '#8b949e',  // grey
};

const STATUS_LABELS_ZH: Record<string, string> = {
    modified: '修改',
    added: '新增',
    deleted: '删除',
    renamed: '重命名',
    untracked: '未跟踪',
};

export default function GitDiffPanel({ lang, onDirtyCountChange }: Props) {
    const [data, setData] = useState<DiffResponse | null>(null);
    const [expanded, setExpanded] = useState<Record<string, boolean>>({});
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState('');

    const fetchDiff = useCallback(async () => {
        try {
            setLoading(true);
            const r = await fetch(`${API_BASE}/api/git/diff`, { cache: 'no-store' });
            if (!r.ok) {
                throw new Error(`HTTP ${r.status}`);
            }
            const j: DiffResponse = await r.json();
            setData(j);
            setErr('');
            if (onDirtyCountChange) onDirtyCountChange(j.files?.length || 0);
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            setErr(msg);
        } finally {
            setLoading(false);
        }
    }, [onDirtyCountChange]);

    useEffect(() => {
        fetchDiff();
        const t = setInterval(fetchDiff, 5000);
        return () => clearInterval(t);
    }, [fetchDiff]);

    return (
        <div className="flex flex-col h-full text-[11px]">
            {/* Header */}
            <div className="px-3 py-2 border-b border-white/5 flex items-center justify-between">
                <div className="font-bold text-[11px] tracking-wider uppercase text-[var(--text2)]">
                    {lang === 'zh' ? '源码控制' : 'Source Control'}
                </div>
                <button
                    onClick={fetchDiff}
                    title={lang === 'zh' ? '刷新' : 'Refresh'}
                    className="px-1.5 py-0.5 rounded hover:bg-white/10 text-[var(--text3)] hover:text-[var(--text1)] transition-colors"
                    style={{ fontSize: 11 }}
                >↻</button>
            </div>

            {/* Summary */}
            {data?.summary && (
                <div className="px-3 py-2 border-b border-white/5 flex items-center gap-3 text-[10px]">
                    <span className="text-[var(--text3)]">
                        {lang === 'zh' ? '变更' : 'Changes'}: <span className="text-[var(--text1)] font-semibold">{data.summary.total_files}</span>
                    </span>
                    <span style={{ color: '#3fb950' }}>+{data.summary.added_lines}</span>
                    <span style={{ color: '#f85149' }}>-{data.summary.deleted_lines}</span>
                </div>
            )}

            {err && (
                <div className="px-3 py-2 text-[10px] text-red-400">
                    {lang === 'zh' ? '加载失败: ' : 'Fetch failed: '}{err}
                </div>
            )}

            {/* File list */}
            <div className="flex-1 overflow-y-auto">
                {data && data.files.length === 0 && (
                    <div className="px-3 py-6 text-center text-[10px] text-[var(--text3)]">
                        {lang === 'zh' ? '工作区干净，无变更' : 'Working tree clean'}
                    </div>
                )}
                {data?.files.map((f) => {
                    const isOpen = !!expanded[f.path];
                    return (
                        <div key={f.path} className="border-b border-white/5">
                            <button
                                onClick={() => setExpanded((s) => ({ ...s, [f.path]: !s[f.path] }))}
                                className="w-full px-3 py-2 flex items-center gap-2 hover:bg-white/5 text-left transition-colors"
                            >
                                <span style={{ fontSize: 9, color: 'var(--text3)', width: 8 }}>{isOpen ? '▾' : '▸'}</span>
                                <span style={{
                                    fontSize: 8,
                                    fontWeight: 700,
                                    color: STATUS_COLORS[f.status],
                                    padding: '1px 4px',
                                    borderRadius: 3,
                                    background: STATUS_COLORS[f.status] + '22',
                                    minWidth: 32,
                                    textAlign: 'center',
                                }}>{lang === 'zh' ? STATUS_LABELS_ZH[f.status] : f.status.slice(0, 1).toUpperCase()}</span>
                                <span className="flex-1 truncate text-[10px]" title={f.path}>{f.path}</span>
                                {f.hunks && f.hunks.length > 0 && (
                                    <span className="text-[8px] text-[var(--text3)] flex gap-1.5">
                                        <span style={{ color: '#3fb950' }}>+{f.hunks.filter(h => h.type === 'add').length}</span>
                                        <span style={{ color: '#f85149' }}>-{f.hunks.filter(h => h.type === 'del').length}</span>
                                    </span>
                                )}
                            </button>
                            {isOpen && f.hunks && f.hunks.length > 0 && (
                                <div style={{ maxHeight: 280, overflowY: 'auto', background: 'rgba(0,0,0,0.3)' }}>
                                    <pre className="text-[10px] font-mono leading-snug px-3 py-2" style={{ margin: 0 }}>
                                        {f.hunks.map((h, idx) => {
                                            let bg = 'transparent';
                                            let color = 'var(--text2)';
                                            let prefix = '  ';
                                            if (h.type === 'add') {
                                                bg = 'rgba(63, 185, 80, 0.15)';
                                                color = '#56d364';
                                                prefix = '+ ';
                                            } else if (h.type === 'del') {
                                                bg = 'rgba(248, 81, 73, 0.15)';
                                                color = '#f85149';
                                                prefix = '- ';
                                            } else if (h.type === 'hunk_header') {
                                                bg = 'rgba(88, 166, 255, 0.08)';
                                                color = '#8b949e';
                                                prefix = '  ';
                                            }
                                            return (
                                                <div
                                                    key={idx}
                                                    style={{
                                                        background: bg,
                                                        color,
                                                        paddingLeft: 4,
                                                        paddingRight: 4,
                                                        whiteSpace: 'pre-wrap',
                                                        wordBreak: 'break-all',
                                                    }}
                                                >
                                                    {prefix}{h.content}
                                                </div>
                                            );
                                        })}
                                    </pre>
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>

            <div className="px-3 py-1.5 border-t border-white/5 text-[9px] text-[var(--text3)]">
                {lang === 'zh' ? '每 5 秒自动刷新' : 'Auto-refresh 5s'} · {data?.project_path?.split('/').slice(-2).join('/') || '—'}
                {loading && <span className="ml-2">…</span>}
            </div>
        </div>
    );
}
