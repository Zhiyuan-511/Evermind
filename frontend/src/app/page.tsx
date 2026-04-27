'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

const API_BASE =
    typeof window !== 'undefined' && (window as any).__EVERMIND_API_BASE__
        ? (window as any).__EVERMIND_API_BASE__
        : 'http://127.0.0.1:8765';

/**
 * Design tokens — strict 8pt grid + 1.25 modular type scale.
 * Spacing:  4 / 8 / 16 / 24 / 32 / 40 / 56 / 64 / 80 / 96
 * Type:     11 / 13 / 16 / 20 / 26 (1.25 ratio rounded)
 * Color:    layered grays — primary / secondary / tertiary / quaternary
 */
const C = {
    bg:     '#0b0d12',
    bgElev: '#13161e',
    border: 'rgba(255,255,255,0.06)',
    borderHi: 'rgba(155,180,255,0.18)',
    primary: '#f0f2f7',
    secondary: '#a8b2c8',
    tertiary: '#6b7589',
    quaternary: '#3f4859',
    accent: '#6f8aff',
    accentSoft: 'rgba(111,138,255,0.10)',
};

interface RecentTask {
    id: string;
    title: string;
    description: string;
    status: string;
    updatedAt?: number;
    runIds?: string[];
    /** v7.3: per-task workspace file count populated by /api/tasks/<id>/workspace */
    fileCount?: number;
}

function formatRelative(ms: number | undefined): string {
    if (!ms) return '';
    const diff = Date.now() - ms;
    if (diff < 60_000) return 'just now';
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
    return `${Math.floor(diff / 86_400_000)}d ago`;
}

function statusTone(status: string): { color: string; label: string } {
    const s = (status || '').toLowerCase();
    if (s === 'done' || s === 'completed' || s === 'passed') return { color: '#5fe3a1', label: 'done' };
    if (s === 'failed' || s === 'cancelled' || s === 'error') return { color: '#ff8a8a', label: s };
    if (s === 'executing' || s === 'running' || s === 'in_progress') return { color: '#ffd166', label: 'running' };
    if (s === 'backlog' || s === 'planned' || s === 'queued') return { color: '#9bb4ff', label: s };
    return { color: C.tertiary, label: s || 'idle' };
}

export default function Home() {
    const [recent, setRecent] = useState<RecentTask[]>([]);
    const [loaded, setLoaded] = useState(false);
    const [showAll, setShowAll] = useState(false);

    useEffect(() => {
        const ctrl = new AbortController();
        (async () => {
            try {
                const r = await fetch(`${API_BASE}/api/tasks?limit=20`, { signal: ctrl.signal });
                if (!r.ok) { setLoaded(true); return; }
                const j = await r.json();
                const list: any[] = Array.isArray(j?.tasks) ? j.tasks : Array.isArray(j) ? j : [];
                const mapped: RecentTask[] = list
                    .map((t) => ({
                        id: String(t?.id || ''),
                        title: String(t?.title || t?.description || '(untitled)').slice(0, 80),
                        description: String(t?.description || '').slice(0, 200),
                        status: String(t?.status || ''),
                        updatedAt: Number(t?.updatedAt || t?.updated_at || 0),
                        runIds: Array.isArray(t?.runIds) ? t.runIds : t?.run_ids,
                    }))
                    .filter((t) => t.id && t.title)
                    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
                setRecent(mapped);
                // v7.3: enrich top 8 visible tasks with workspace file count
                // (best-effort, ignore failures so launchpad still loads).
                Promise.allSettled(
                    mapped.slice(0, 8).map(async (t) => {
                        try {
                            const wr = await fetch(`${API_BASE}/api/tasks/${encodeURIComponent(t.id)}/workspace`, { signal: ctrl.signal });
                            if (!wr.ok) return null;
                            const wj = await wr.json();
                            const fc = Number(wj?.stats?.file_count || 0);
                            return { id: t.id, fc };
                        } catch { return null; }
                    }),
                ).then((results) => {
                    const fcMap = new Map<string, number>();
                    for (const res of results) {
                        if (res.status === 'fulfilled' && res.value) {
                            fcMap.set(res.value.id, res.value.fc);
                        }
                    }
                    if (fcMap.size > 0) {
                        setRecent((prev) =>
                            prev.map((t) => (fcMap.has(t.id) ? { ...t, fileCount: fcMap.get(t.id)! } : t)),
                        );
                    }
                }).catch(() => { /* ignore */ });
            } catch { /* offline backend ok */ }
            finally { setLoaded(true); }
        })();
        return () => ctrl.abort();
    }, []);

    const visible = showAll ? recent : recent.slice(0, 5);

    return (
        <div
            style={{
                minHeight: '100vh',
                background: C.bg,
                color: C.primary,
                fontFamily: '-apple-system, "SF Pro Display", "PingFang SC", "Inter", system-ui, sans-serif',
                fontFeatureSettings: '"ss01", "cv01", "cv11"',
                position: 'relative',
                overflow: 'hidden',
            }}
        >
            {/* Atmosphere */}
            <div aria-hidden style={{
                position: 'absolute', width: 640, height: 640,
                top: -240, right: -160,
                background: 'radial-gradient(circle, rgba(111,138,255,0.14) 0%, transparent 60%)',
                filter: 'blur(40px)', pointerEvents: 'none',
            }} />
            <div aria-hidden style={{
                position: 'absolute', width: 480, height: 480,
                bottom: -180, left: -120,
                background: 'radial-gradient(circle, rgba(178,140,255,0.10) 0%, transparent 65%)',
                filter: 'blur(40px)', pointerEvents: 'none',
            }} />
            {/* Hairline grid */}
            <div aria-hidden style={{
                position: 'absolute', inset: 0,
                backgroundImage: 'linear-gradient(rgba(255,255,255,0.018) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.018) 1px, transparent 1px)',
                backgroundSize: '32px 32px',
                maskImage: 'radial-gradient(ellipse at 50% 35%, #000 0%, transparent 70%)',
                WebkitMaskImage: 'radial-gradient(ellipse at 50% 35%, #000 0%, transparent 70%)',
                pointerEvents: 'none',
            }} />

            <main
                style={{
                    position: 'relative', zIndex: 1,
                    minHeight: '100vh',
                    display: 'flex', flexDirection: 'column', alignItems: 'center',
                    padding: '96px 24px 48px',
                }}
            >
                <div
                    style={{
                        width: '100%',
                        maxWidth: 480,
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'stretch',
                        animation: 'fadeUp 0.55s cubic-bezier(0.2, 0.8, 0.2, 1) both',
                    }}
                >
                    {/* Brand block */}
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', marginBottom: 56 }}>
                        <BrandMark />
                        <h1 style={{
                            marginTop: 24,
                            fontSize: 26,
                            fontWeight: 350,
                            letterSpacing: '-0.01em',
                            lineHeight: 1.1,
                            color: C.primary,
                        }}>
                            evermind
                        </h1>
                        <p style={{
                            marginTop: 8,
                            fontSize: 11,
                            fontWeight: 500,
                            letterSpacing: '0.16em',
                            color: C.tertiary,
                            textTransform: 'uppercase',
                        }}>
                            Multi-Agent Orchestration
                        </p>
                    </div>

                    {/* Primary CTA — go straight into the editor with a blank canvas */}
                    <Link
                        href="/editor"
                        style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            gap: 8,
                            height: 44,
                            padding: '0 24px',
                            borderRadius: 10,
                            background: C.accent,
                            color: '#fff',
                            fontSize: 13,
                            fontWeight: 500,
                            letterSpacing: '0',
                            boxShadow: '0 1px 0 rgba(255,255,255,0.18) inset, 0 6px 24px rgba(111,138,255,0.32)',
                            transition: 'all 0.18s ease',
                            textDecoration: 'none',
                        }}
                        onMouseEnter={(e) => {
                            (e.currentTarget as HTMLElement).style.background = '#7d96ff';
                            (e.currentTarget as HTMLElement).style.transform = 'translateY(-1px)';
                            (e.currentTarget as HTMLElement).style.boxShadow = '0 1px 0 rgba(255,255,255,0.22) inset, 0 10px 28px rgba(111,138,255,0.40)';
                        }}
                        onMouseLeave={(e) => {
                            (e.currentTarget as HTMLElement).style.background = C.accent;
                            (e.currentTarget as HTMLElement).style.transform = 'translateY(0)';
                            (e.currentTarget as HTMLElement).style.boxShadow = '0 1px 0 rgba(255,255,255,0.18) inset, 0 6px 24px rgba(111,138,255,0.32)';
                        }}
                    >
                        <SparkleIcon />
                        New Task
                    </Link>

                    {/* Secondary actions — open the corresponding panel inside the editor */}
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 8 }}>
                        <SecondaryAction
                            href="/editor?panel=templates"
                            icon={<TemplateIcon />}
                            label="Templates"
                        />
                        <SecondaryAction
                            href="/editor?panel=github"
                            icon={<GitIcon />}
                            label="Clone Repo"
                        />
                    </div>

                    {/* Recent */}
                    {loaded && (
                        <section style={{ marginTop: 64 }}>
                            <header style={{
                                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                                padding: '0 4px', marginBottom: 16,
                            }}>
                                <span style={{
                                    fontSize: 10,
                                    fontWeight: 600,
                                    letterSpacing: '0.16em',
                                    textTransform: 'uppercase',
                                    color: C.tertiary,
                                }}>
                                    Recent
                                </span>
                                {recent.length > 0 && (
                                    <span style={{
                                        fontSize: 10, fontWeight: 500,
                                        letterSpacing: '0.08em',
                                        color: C.quaternary,
                                    }}>
                                        {recent.length}
                                    </span>
                                )}
                            </header>

                            {visible.length === 0 ? (
                                <div style={{
                                    padding: '32px 24px',
                                    borderRadius: 12,
                                    background: 'rgba(255,255,255,0.015)',
                                    border: `1px dashed ${C.border}`,
                                    fontSize: 13,
                                    lineHeight: 1.5,
                                    color: C.tertiary,
                                    textAlign: 'center',
                                }}>
                                    No tasks yet. Click <span style={{ color: C.secondary }}>Open Workspace</span> to start.
                                </div>
                            ) : (
                                <ul style={{ display: 'flex', flexDirection: 'column', gap: 4, listStyle: 'none', padding: 0, margin: 0 }}>
                                    {visible.map((t, i) => {
                                        const tone = statusTone(t.status);
                                        return (
                                            <li key={t.id} style={{ animation: `fadeUp 0.4s ease-out ${0.05 + i * 0.03}s both` }}>
                                                <Link
                                                    href={`/editor?task=${encodeURIComponent(t.id)}`}
                                                    style={{
                                                        display: 'flex',
                                                        alignItems: 'center',
                                                        gap: 12,
                                                        padding: '12px 16px',
                                                        borderRadius: 10,
                                                        background: 'rgba(255,255,255,0.022)',
                                                        border: `1px solid ${C.border}`,
                                                        textDecoration: 'none',
                                                        transition: 'all 0.15s ease',
                                                    }}
                                                    onMouseEnter={(e) => {
                                                        (e.currentTarget as HTMLElement).style.background = 'rgba(155,180,255,0.05)';
                                                        (e.currentTarget as HTMLElement).style.borderColor = C.borderHi;
                                                    }}
                                                    onMouseLeave={(e) => {
                                                        (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.022)';
                                                        (e.currentTarget as HTMLElement).style.borderColor = C.border;
                                                    }}
                                                >
                                                    <span aria-hidden style={{
                                                        width: 6, height: 6, borderRadius: 999,
                                                        background: tone.color,
                                                        boxShadow: `0 0 8px ${tone.color}`,
                                                        flexShrink: 0,
                                                    }} />
                                                    <div style={{ minWidth: 0, flex: 1 }}>
                                                        <div style={{
                                                            fontSize: 13,
                                                            fontWeight: 400,
                                                            lineHeight: 1.4,
                                                            color: C.primary,
                                                            whiteSpace: 'nowrap',
                                                            overflow: 'hidden',
                                                            textOverflow: 'ellipsis',
                                                        }}>
                                                            {t.title}
                                                        </div>
                                                        {t.description && t.description !== t.title && (
                                                            <div style={{
                                                                fontSize: 11,
                                                                lineHeight: 1.4,
                                                                color: C.tertiary,
                                                                marginTop: 2,
                                                                whiteSpace: 'nowrap',
                                                                overflow: 'hidden',
                                                                textOverflow: 'ellipsis',
                                                            }}>
                                                                {t.description.slice(0, 100)}
                                                            </div>
                                                        )}
                                                    </div>
                                                    {typeof t.fileCount === 'number' && t.fileCount > 0 && (
                                                        <span
                                                            title={`${t.fileCount} file${t.fileCount === 1 ? '' : 's'} in workspace`}
                                                            style={{
                                                                display: 'inline-flex', alignItems: 'center', gap: 4,
                                                                fontSize: 10, fontWeight: 500,
                                                                color: C.secondary,
                                                                background: 'rgba(155,180,255,0.06)',
                                                                padding: '2px 6px',
                                                                borderRadius: 4,
                                                                flexShrink: 0,
                                                            }}
                                                        >
                                                            <svg width="9" height="9" viewBox="0 0 16 16" fill="none">
                                                                <path d="M2 4.5C2 3.67 2.67 3 3.5 3h3.1l1.6 2H12.5C13.33 5 14 5.67 14 6.5v5C14 12.33 13.33 13 12.5 13h-9C2.67 13 2 12.33 2 11.5v-7Z" stroke="currentColor" strokeWidth="1.4" />
                                                            </svg>
                                                            {t.fileCount}
                                                        </span>
                                                    )}
                                                    <span style={{
                                                        fontSize: 10, fontWeight: 500,
                                                        letterSpacing: '0.04em',
                                                        color: C.tertiary,
                                                        whiteSpace: 'nowrap',
                                                        flexShrink: 0,
                                                    }}>
                                                        {formatRelative(t.updatedAt)}
                                                    </span>
                                                </Link>
                                            </li>
                                        );
                                    })}
                                </ul>
                            )}

                            {recent.length > 5 && (
                                <button
                                    type="button"
                                    onClick={() => setShowAll((v) => !v)}
                                    style={{
                                        width: '100%',
                                        marginTop: 16,
                                        padding: '8px 0',
                                        background: 'transparent',
                                        border: 'none',
                                        fontSize: 11,
                                        fontWeight: 500,
                                        letterSpacing: '0.08em',
                                        color: C.quaternary,
                                        textTransform: 'uppercase',
                                        cursor: 'pointer',
                                        transition: 'color 0.15s',
                                    }}
                                    onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = C.secondary)}
                                    onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = C.quaternary)}
                                >
                                    {showAll ? 'Show less' : `Show ${recent.length - 5} more`}
                                </button>
                            )}
                        </section>
                    )}
                </div>

                {/* Footer mark */}
                <footer style={{
                    marginTop: 'auto',
                    paddingTop: 80,
                    fontSize: 10,
                    fontWeight: 500,
                    letterSpacing: '0.2em',
                    color: C.quaternary,
                    textTransform: 'uppercase',
                }}>
                    v3.0
                </footer>
            </main>

            <style jsx>{`
                @keyframes fadeUp {
                    from { opacity: 0; transform: translateY(8px); }
                    to { opacity: 1; transform: translateY(0); }
                }
            `}</style>
        </div>
    );
}

function SecondaryAction({ href, icon, label }: { href: string; icon: React.ReactNode; label: string }) {
    return (
        <Link
            href={href}
            style={{
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 8,
                height: 40,
                borderRadius: 10,
                background: 'rgba(255,255,255,0.022)',
                border: `1px solid ${C.border}`,
                color: C.secondary,
                fontSize: 13,
                fontWeight: 400,
                textDecoration: 'none',
                transition: 'all 0.15s',
            }}
            onMouseEnter={(e) => {
                (e.currentTarget as HTMLElement).style.background = 'rgba(155,180,255,0.05)';
                (e.currentTarget as HTMLElement).style.borderColor = C.borderHi;
                (e.currentTarget as HTMLElement).style.color = C.primary;
            }}
            onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.022)';
                (e.currentTarget as HTMLElement).style.borderColor = C.border;
                (e.currentTarget as HTMLElement).style.color = C.secondary;
            }}
        >
            {icon}
            {label}
        </Link>
    );
}

/* ─── Icons ────────────────────────────────────────────────────── */

function SparkleIcon() {
    return (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <path d="M8 1.5l1.5 4.2L13.5 7l-4 1.3L8 12.5 6.5 8.3 2.5 7l4-1.3L8 1.5Z" fill="currentColor" />
            <circle cx="13" cy="3" r="0.9" fill="currentColor" opacity="0.7" />
            <circle cx="2.6" cy="13" r="0.7" fill="currentColor" opacity="0.55" />
        </svg>
    );
}
function TemplateIcon() {
    return (
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
            <rect x="2" y="2.5" width="6" height="6" rx="1" stroke="currentColor" strokeWidth="1.3" />
            <rect x="9" y="2.5" width="5" height="3" rx="1" stroke="currentColor" strokeWidth="1.3" />
            <rect x="9" y="6.5" width="5" height="7" rx="1" stroke="currentColor" strokeWidth="1.3" />
            <rect x="2" y="9.5" width="6" height="4" rx="1" stroke="currentColor" strokeWidth="1.3" />
        </svg>
    );
}
function GitIcon() {
    return (
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
            <circle cx="4" cy="4" r="1.6" stroke="currentColor" strokeWidth="1.3" />
            <circle cx="4" cy="12" r="1.6" stroke="currentColor" strokeWidth="1.3" />
            <circle cx="12" cy="8" r="1.6" stroke="currentColor" strokeWidth="1.3" />
            <path d="M4 5.6v4.8M5.6 4.7c2 0 3 1.2 3 3.3M5.6 11.3c2 0 3-1.2 3-3.3" stroke="currentColor" strokeWidth="1.3" />
        </svg>
    );
}

/**
 * Brand mark — orchestration constellation.
 * Six agent nodes around a luminous core. Conveys multi-agent identity
 * without resembling any letter. 64×64 to fit a tight 8pt rhythm.
 */
function BrandMark() {
    return (
        <div style={{ position: 'relative', width: 64, height: 64 }}>
            <svg width="64" height="64" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
                <defs>
                    <linearGradient id="ev-edge" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
                        <stop offset="0%" stopColor="#6f8aff" stopOpacity="0.55" />
                        <stop offset="100%" stopColor="#b88dff" stopOpacity="0.55" />
                    </linearGradient>
                    <radialGradient id="ev-core" cx="0.5" cy="0.5" r="0.5">
                        <stop offset="0%" stopColor="#ffffff" />
                        <stop offset="45%" stopColor="#c8b6ff" />
                        <stop offset="100%" stopColor="#6f8aff" stopOpacity="0.4" />
                    </radialGradient>
                    <radialGradient id="ev-node" cx="0.5" cy="0.5" r="0.5">
                        <stop offset="0%" stopColor="#a8b8ff" />
                        <stop offset="100%" stopColor="#6f8aff" />
                    </radialGradient>
                </defs>
                {/* Spokes */}
                <g stroke="url(#ev-edge)" strokeWidth="0.9">
                    <line x1="32" y1="32" x2="32" y2="10" />
                    <line x1="32" y1="32" x2="51" y2="21" />
                    <line x1="32" y1="32" x2="51" y2="43" />
                    <line x1="32" y1="32" x2="32" y2="54" />
                    <line x1="32" y1="32" x2="13" y2="43" />
                    <line x1="32" y1="32" x2="13" y2="21" />
                </g>
                {/* Outer ring */}
                <g stroke="url(#ev-edge)" strokeWidth="0.6" strokeOpacity="0.5">
                    <line x1="32" y1="10" x2="51" y2="21" />
                    <line x1="51" y1="21" x2="51" y2="43" />
                    <line x1="51" y1="43" x2="32" y2="54" />
                    <line x1="32" y1="54" x2="13" y2="43" />
                    <line x1="13" y1="43" x2="13" y2="21" />
                    <line x1="13" y1="21" x2="32" y2="10" />
                </g>
                {/* Halo */}
                <circle cx="32" cy="32" r="11" fill="none" stroke="#c8b6ff" strokeOpacity="0.14" strokeWidth="1" />
                {/* Core */}
                <circle cx="32" cy="32" r="7" fill="url(#ev-core)" />
                {/* Outer agents */}
                <circle cx="32" cy="10" r="2.6" fill="url(#ev-node)" />
                <circle cx="51" cy="21" r="2.6" fill="url(#ev-node)" />
                <circle cx="51" cy="43" r="2.6" fill="url(#ev-node)" />
                <circle cx="32" cy="54" r="2.6" fill="url(#ev-node)" />
                <circle cx="13" cy="43" r="2.6" fill="url(#ev-node)" />
                <circle cx="13" cy="21" r="2.6" fill="url(#ev-node)" />
            </svg>
            <div aria-hidden style={{
                position: 'absolute', inset: -16,
                background: 'radial-gradient(circle, rgba(111,138,255,0.18) 0%, transparent 65%)',
                filter: 'blur(8px)', pointerEvents: 'none', zIndex: -1,
            }} />
        </div>
    );
}
