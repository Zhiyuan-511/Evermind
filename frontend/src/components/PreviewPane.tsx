'use client';

import { useState, useCallback } from 'react';

interface PreviewPaneProps {
    previewUrl: string | null;
    lang: 'en' | 'zh';
    onClose: () => void;
}

export default function PreviewPane({ previewUrl, lang, onClose }: PreviewPaneProps) {
    const [expanded, setExpanded] = useState(false);
    const [collapsed, setCollapsed] = useState(false);
    const [iframeKey, setIframeKey] = useState(0);

    const t = useCallback((en: string, zh: string) => (lang === 'zh' ? zh : en), [lang]);

    const handleReload = () => setIframeKey(k => k + 1);

    const handleOpenExternal = () => {
        if (previewUrl) {
            try {
                window.open(previewUrl, '_blank', 'noopener,noreferrer');
            } catch {
                navigator.clipboard?.writeText(previewUrl);
            }
        }
    };

    if (!previewUrl) return null;

    // Fullscreen mode
    if (expanded) {
        return (
            <div style={{
                position: 'fixed', inset: 0, zIndex: 9999,
                display: 'flex', flexDirection: 'column',
                background: 'var(--bg1)',
                animation: 'fadeIn 0.15s ease-out',
            }}>
                <div style={{
                    display: 'flex', alignItems: 'center', gap: 8,
                    padding: '8px 14px',
                    background: 'var(--surface-strong)',
                    borderBottom: '1px solid var(--glass-border)',
                }}>
                    <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text1)', flex: 1 }}>
                        🔗 {t('Live Preview', '实时预览')}
                    </span>
                    <button onClick={handleReload} className="btn text-[11px]">🔄 {t('Reload', '刷新')}</button>
                    <button onClick={handleOpenExternal} className="btn text-[11px]">🌐 {t('Open', '打开')}</button>
                    <button onClick={() => setExpanded(false)} className="btn text-[11px]">⬜ {t('Exit', '退出')}</button>
                </div>
                <iframe key={iframeKey} src={previewUrl} title="Preview"
                    sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                    style={{ flex: 1, width: '100%', border: 'none', background: '#fff' }} />
            </div>
        );
    }

    // Collapsed tab — minimal floating indicator
    if (collapsed) {
        return (
            <div
                onClick={() => setCollapsed(false)}
                style={{
                    position: 'absolute', bottom: 8, right: 8,
                    padding: '6px 12px', borderRadius: 20,
                    background: 'var(--surface-strong)',
                    border: '1px solid var(--glass-border)',
                    backdropFilter: 'blur(12px)',
                    fontSize: 10, fontWeight: 600,
                    color: 'var(--blue)',
                    cursor: 'pointer',
                    display: 'flex', alignItems: 'center', gap: 6,
                    boxShadow: '0 4px 16px rgba(0,0,0,0.3)',
                    transition: 'all 0.2s',
                    zIndex: 50,
                }}
            >
                🔗 {t('Preview', '预览')}
                <span style={{
                    width: 6, height: 6, borderRadius: '50%',
                    background: 'var(--green)',
                    boxShadow: '0 0 6px var(--green)',
                }} />
            </div>
        );
    }

    // Normal inline mode — glassmorphism panel
    return (
        <div style={{
            display: 'flex', flexDirection: 'column',
            height: 260,
            borderTop: '1px solid var(--glass-border)',
            background: 'var(--surface)',
            animation: 'slideUp 0.25s ease-out',
            overflow: 'hidden',
        }}>
            {/* Glass toolbar */}
            <div style={{
                display: 'flex', alignItems: 'center', gap: 6,
                padding: '5px 10px',
                background: 'rgba(255,255,255,0.03)',
                borderBottom: '1px solid rgba(255,255,255,0.06)',
                backdropFilter: 'blur(8px)',
                flexShrink: 0,
            }}>
                <span style={{
                    fontSize: 10, fontWeight: 700,
                    color: 'var(--text1)', flex: 1,
                    display: 'flex', alignItems: 'center', gap: 5,
                }}>
                    🔗 {t('Live Preview', '实时预览')}
                    <span style={{
                        width: 5, height: 5, borderRadius: '50%',
                        background: 'var(--green)',
                        boxShadow: '0 0 4px var(--green)',
                    }} />
                </span>
                <button onClick={handleReload} className="btn text-[9px]" title={t('Reload', '刷新')}>🔄</button>
                <button onClick={handleOpenExternal} className="btn text-[9px]" title={t('Open in browser', '在浏览器中打开')}>🌐</button>
                <button onClick={() => setExpanded(true)} className="btn text-[9px]" title={t('Fullscreen', '全屏')}>⬛</button>
                <button onClick={() => setCollapsed(true)} className="btn text-[9px]" title={t('Minimize', '最小化')}>▼</button>
                <button onClick={onClose} className="btn text-[9px]" title={t('Close', '关闭')}>✕</button>
            </div>

            {/* Iframe */}
            <iframe
                key={iframeKey}
                src={previewUrl}
                title="Preview"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                style={{
                    flex: 1, width: '100%', border: 'none',
                    background: '#fff',
                    borderRadius: '0 0 6px 6px',
                }}
            />

            {/* Subtle URL bar */}
            <div style={{
                padding: '3px 10px',
                fontSize: 8, color: 'var(--text3)',
                borderTop: '1px solid rgba(255,255,255,0.04)',
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                flexShrink: 0,
                background: 'rgba(0,0,0,0.15)',
            }}>
                {previewUrl.replace(/[?&]_ts=\d+/, '')}
            </div>
        </div>
    );
}
