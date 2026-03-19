'use client';

import React, { memo, useCallback } from 'react';

interface PreviewCenterProps {
    previewUrl: string | null;
    onRefresh: () => void;
    onClose: () => void;
    onNewWindow: () => void;
    lang: 'en' | 'zh';
    runId?: string;
    taskTitle?: string;
    running?: boolean;
}

function stripCacheBust(url: string): string {
    return url
        .replace(/([?&])_ts=\d+(&?)/, (_m, p1: string, p2: string) => (p1 === '?' && p2 ? '?' : p1))
        .replace(/[?&]$/, '');
}

function shorten(value: string, max = 26): string {
    const compact = value.trim();
    if (compact.length <= max) return compact;
    return `${compact.slice(0, max - 1)}…`;
}

function PreviewCenter({
    previewUrl,
    onRefresh,
    onClose,
    onNewWindow,
    lang,
    runId,
    taskTitle,
    running,
}: PreviewCenterProps) {
    const tr = useCallback((zh: string, en: string) => (lang === 'zh' ? zh : en), [lang]);

    const handleExportPdf = useCallback(async () => {
        try {
            const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
            const res = await fetch(`${apiBase}/api/export-pdf`);
            if (!res.ok) throw new Error('PDF export failed');
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = res.headers.get('content-disposition')?.match(/filename="(.+)"/)?.[1] || 'export.pdf';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch {
            const iframe = document.querySelector('iframe[title="Website Preview"]') as HTMLIFrameElement | null;
            if (iframe?.contentWindow) {
                try {
                    iframe.contentWindow.print();
                } catch {
                    window.open(stripCacheBust(previewUrl || ''), '_blank')?.print();
                }
            }
        }
    }, [previewUrl]);

    if (!previewUrl) {
        return (
            <div style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                width: '100%',
                height: '100%',
                padding: 32,
                color: 'var(--text3)',
                background:
                    'radial-gradient(circle at 20% 20%, rgba(79,143,255,0.08), transparent 34%), linear-gradient(180deg, var(--canvas-bg) 0%, color-mix(in srgb, var(--canvas-bg) 78%, black 22%) 100%)',
            }}>
                <div style={{
                    width: 'min(420px, 90%)',
                    borderRadius: 18,
                    border: '1px solid var(--glass-border)',
                    background: 'var(--surface-strong)',
                    boxShadow: '0 18px 48px rgba(0,0,0,0.22)',
                    overflow: 'hidden',
                }}>
                    <div style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        padding: '12px 14px',
                        borderBottom: '1px solid rgba(255,255,255,0.06)',
                        background: 'color-mix(in srgb, var(--surface-strong) 92%, white 8%)',
                    }}>
                        {['#ff6f61', '#f4b400', '#34a853'].map((color) => (
                            <span
                                key={color}
                                style={{
                                    width: 8,
                                    height: 8,
                                    borderRadius: '50%',
                                    background: color,
                                    opacity: 0.9,
                                }}
                            />
                        ))}
                        <div style={{
                            marginLeft: 8,
                            flex: 1,
                            height: 10,
                            borderRadius: 999,
                            background: 'rgba(255,255,255,0.05)',
                            border: '1px solid rgba(255,255,255,0.06)',
                        }} />
                    </div>
                    <div style={{ padding: 20, display: 'grid', gap: 12 }}>
                        <div style={{
                            height: 120,
                            borderRadius: 12,
                            border: '1px dashed rgba(79,143,255,0.28)',
                            background:
                                'linear-gradient(180deg, rgba(79,143,255,0.08), rgba(79,143,255,0.02)), repeating-linear-gradient(90deg, transparent, transparent 22px, rgba(255,255,255,0.03) 22px, rgba(255,255,255,0.03) 23px)',
                        }} />
                        <div style={{ display: 'grid', gap: 8 }}>
                            <div style={{ width: '62%', height: 10, borderRadius: 999, background: 'rgba(255,255,255,0.06)' }} />
                            <div style={{ width: '100%', height: 8, borderRadius: 999, background: 'rgba(255,255,255,0.04)' }} />
                            <div style={{ width: '82%', height: 8, borderRadius: 999, background: 'rgba(255,255,255,0.04)' }} />
                        </div>
                    </div>
                </div>

                <div style={{
                    marginTop: 22,
                    fontSize: 16,
                    fontWeight: 600,
                    color: 'var(--text2)',
                }}>
                    {tr('暂无预览', 'No preview yet')}
                </div>
                <div style={{
                    marginTop: 8,
                    maxWidth: 360,
                    textAlign: 'center',
                    lineHeight: 1.6,
                    fontSize: 12,
                }}>
                    {tr(
                        '运行一个任务后，这里会显示生成结果。预览准备完成时会自动切换到这个视图。',
                        'Run a task to generate output. This view switches in automatically once the preview is ready.',
                    )}
                </div>
                <button
                    onClick={onClose}
                    className="btn btn-primary text-[11px]"
                    style={{ marginTop: 18 }}
                >
                    {tr('返回编辑器', 'Back to editor')}
                </button>
            </div>
        );
    }

    const cleanPreviewUrl = stripCacheBust(previewUrl);
    const contextItems = [
        taskTitle ? `${tr('任务', 'Task')}: ${shorten(taskTitle, 32)}` : '',
        runId ? `${tr('运行', 'Run')}: ${shorten(runId, 16)}` : '',
    ].filter(Boolean);

    return (
        <div style={{
            display: 'flex',
            flexDirection: 'column',
            width: '100%',
            height: '100%',
            animation: 'fadeIn 0.2s ease-out',
            background: 'var(--canvas-bg)',
        }}>
            <div style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '10px 14px',
                background: 'var(--surface-strong)',
                borderBottom: '1px solid var(--glass-border)',
                flexShrink: 0,
            }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    {['#ff6f61', '#f4b400', '#34a853'].map((color) => (
                        <span
                            key={color}
                            style={{
                                width: 8,
                                height: 8,
                                borderRadius: '50%',
                                background: color,
                                opacity: 0.9,
                            }}
                        />
                    ))}
                </div>

                <div style={{
                    minWidth: 0,
                    flex: 1,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    overflow: 'hidden',
                }}>
                    <span style={{
                        fontSize: 12,
                        fontWeight: 700,
                        letterSpacing: '0.02em',
                        color: 'var(--text1)',
                        whiteSpace: 'nowrap',
                    }}>
                        {tr('网站预览', 'Website Preview')}
                    </span>
                    <span style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 6,
                        padding: '2px 8px',
                        borderRadius: 999,
                        border: '1px solid rgba(255,255,255,0.08)',
                        background: running ? 'rgba(79,143,255,0.10)' : 'rgba(64,214,124,0.10)',
                        color: running ? 'var(--blue)' : 'var(--green)',
                        fontSize: 10,
                        fontWeight: 600,
                        whiteSpace: 'nowrap',
                    }}>
                        <span style={{
                            width: 6,
                            height: 6,
                            borderRadius: '50%',
                            background: running ? 'var(--blue)' : 'var(--green)',
                            boxShadow: running ? '0 0 8px rgba(79,143,255,0.45)' : '0 0 8px rgba(64,214,124,0.35)',
                            animation: running ? 'previewPulse 1.5s infinite' : 'none',
                        }} />
                        {running ? tr('实时', 'Live') : tr('稳定', 'Stable')}
                    </span>
                    {contextItems.length > 0 && (
                        <span style={{
                            minWidth: 0,
                            padding: '2px 8px',
                            borderRadius: 999,
                            border: '1px solid rgba(255,255,255,0.08)',
                            background: 'rgba(255,255,255,0.04)',
                            color: 'var(--text3)',
                            fontSize: 10,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                        }}>
                            {contextItems.join(' · ')}
                        </span>
                    )}
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <button
                        onClick={onRefresh}
                        className="btn text-[10px]"
                        title={tr('刷新预览', 'Refresh preview')}
                    >
                        {tr('刷新', 'Reload')}
                    </button>
                    <button
                        onClick={onNewWindow}
                        className="btn text-[10px]"
                        title={tr('在新窗口中打开', 'Open in new window')}
                    >
                        {tr('新窗口', 'Open')}
                    </button>
                    <button
                        onClick={handleExportPdf}
                        className="btn text-[10px]"
                        title={tr('导出 PDF', 'Export PDF')}
                    >
                        PDF
                    </button>
                    <button
                        onClick={onClose}
                        className="btn text-[10px]"
                    >
                        {tr('返回画布', 'Canvas')}
                    </button>
                </div>
            </div>

            <div style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '8px 14px',
                borderBottom: '1px solid rgba(255,255,255,0.05)',
                background: 'rgba(0,0,0,0.12)',
                flexShrink: 0,
            }}>
                <span style={{
                    fontSize: 9,
                    fontWeight: 700,
                    letterSpacing: '0.08em',
                    color: 'var(--text3)',
                    textTransform: 'uppercase',
                }}>
                    {tr('地址', 'Address')}
                </span>
                <div style={{
                    minWidth: 0,
                    flex: 1,
                    padding: '6px 10px',
                    borderRadius: 999,
                    border: '1px solid rgba(255,255,255,0.08)',
                    background: 'rgba(255,255,255,0.04)',
                    color: 'var(--text2)',
                    fontSize: 10,
                    fontFamily: "var(--font-mono), 'JetBrains Mono', monospace",
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                }}>
                    {cleanPreviewUrl}
                </div>
            </div>

            <iframe
                key={previewUrl}
                src={previewUrl}
                title="Website Preview"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                allow="keyboard-map; gamepad"
                tabIndex={0}
                onLoad={(e) => {
                    const frame = e.currentTarget;
                    frame.focus();
                    try { frame.contentWindow?.focus(); } catch { /* cross-origin */ }
                }}
                onClick={(e) => {
                    const frame = e.currentTarget;
                    frame.focus();
                    try { frame.contentWindow?.focus(); } catch { /* cross-origin */ }
                }}
                style={{
                    flex: 1,
                    width: '100%',
                    border: 'none',
                    background: '#fff',
                    outline: 'none',
                }}
            />

            <style>{`
                @keyframes previewPulse {
                    0%, 100% { opacity: 1; }
                    50% { opacity: 0.35; }
                }
            `}</style>
        </div>
    );
}

export default memo(PreviewCenter);
