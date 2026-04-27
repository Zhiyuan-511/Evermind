/**
 * v6.4.29 (maintainer 2026-04-22) — Collapsible "thinking" bubble.
 * Shows reasoning_content (model's internal chain-of-thought) as a small,
 * foldable block above the main answer. Pattern from:
 *   - Claude web UI: "Thinking… [spinner]" then "Thought for 8s ▸ (click to expand)"
 *   - Vercel AI Elements reasoning.tsx
 *   - Anthropic extended-thinking docs
 *
 * Design goals:
 *   1. Zero extra deps (no framer-motion; uses CSS max-height transition)
 *   2. Streams live while `active=true`, auto-collapses when `active` flips false
 *   3. Click header to toggle; keyboard-accessible (button[aria-expanded])
 *   4. Chinese + English locale-aware labels
 */
import { useEffect, useRef, useState } from 'react';

interface ReasoningBlockProps {
    text: string;
    active: boolean;
    startedAt?: number;
    endedAt?: number;
}

function formatElapsed(startedAt: number | undefined, endedAt: number | undefined): string {
    if (!startedAt) return '';
    const end = endedAt || Date.now();
    const sec = Math.max(1, Math.round((end - startedAt) / 1000));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

function detectLang(text: string): 'zh' | 'en' {
    // If any CJK character appears in the first 200 chars, assume Chinese UI.
    return /[一-鿿]/.test((text || '').slice(0, 200)) ? 'zh' : 'en';
}

export default function ReasoningBlock({ text, active, startedAt, endedAt }: ReasoningBlockProps) {
    const [expanded, setExpanded] = useState(true); // open while streaming
    const contentRef = useRef<HTMLDivElement>(null);
    const [contentHeight, setContentHeight] = useState<number>(0);

    // When reasoning finishes, auto-collapse after a 1s grace period (matches
    // Vercel AI Elements pattern — avoids flicker when content arrives right
    // after reasoning stops).
    useEffect(() => {
        if (active) {
            setExpanded(true);
            return;
        }
        const t = setTimeout(() => setExpanded(false), 1000);
        return () => clearTimeout(t);
    }, [active]);

    // Measure content for smooth height animation
    useEffect(() => {
        if (contentRef.current) {
            setContentHeight(contentRef.current.scrollHeight);
        }
    }, [text, expanded]);

    const lang = detectLang(text);
    const label = active
        ? (lang === 'zh' ? '思考中…' : 'Thinking…')
        : (lang === 'zh'
            ? `已思考 ${formatElapsed(startedAt, endedAt)} · 点击${expanded ? '收起' : '展开'}`
            : `Thought for ${formatElapsed(startedAt, endedAt)} · click to ${expanded ? 'collapse' : 'expand'}`);

    if (!text && !active) return null;

    return (
        <div className="reasoning-block" style={{
            margin: '4px 0 8px 0',
            padding: '0',
            borderLeft: '2px solid #a0a0a8',
            paddingLeft: '10px',
            opacity: 0.85,
        }}>
            <button
                type="button"
                aria-expanded={expanded}
                onClick={() => !active && setExpanded(!expanded)}
                disabled={active}
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    fontSize: '12px',
                    color: 'var(--color-muted, #8a8a93)',
                    background: 'transparent',
                    border: 'none',
                    padding: '2px 0',
                    cursor: active ? 'default' : 'pointer',
                    userSelect: 'none',
                    fontFamily: 'inherit',
                }}
            >
                {active ? (
                    <span
                        aria-hidden
                        style={{
                            display: 'inline-block',
                            width: 10,
                            height: 10,
                            border: '1.5px solid currentColor',
                            borderTopColor: 'transparent',
                            borderRadius: '50%',
                            animation: 'reasoning-spin 0.9s linear infinite',
                        }}
                    />
                ) : (
                    <span aria-hidden style={{ fontSize: '10px' }}>{expanded ? '▾' : '▸'}</span>
                )}
                <span>{label}</span>
            </button>
            <div
                aria-hidden={!expanded}
                style={{
                    maxHeight: expanded ? `${Math.max(contentHeight, 40)}px` : '0px',
                    overflow: 'hidden',
                    transition: 'max-height 220ms ease-in-out, opacity 180ms ease-in-out',
                    opacity: expanded ? 1 : 0,
                }}
            >
                <div
                    ref={contentRef}
                    style={{
                        paddingTop: '6px',
                        fontSize: '12px',
                        lineHeight: 1.5,
                        color: 'var(--color-muted-strong, #7a7a85)',
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        fontFamily: 'inherit',
                    }}
                >
                    {text}
                </div>
            </div>
            <style>{`
                @keyframes reasoning-spin {
                    to { transform: rotate(360deg); }
                }
            `}</style>
        </div>
    );
}
