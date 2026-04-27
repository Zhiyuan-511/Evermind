/**
 * v6.4.32 (maintainer 2026-04-23) — Collapsible tool-call badge.
 * Renders a single file_ops / browser / shell tool invocation as a
 * compact badge "[file_ops read /tmp/.../index.html · 66KB ✓]" that
 * expands to show full args + result JSON on click.
 *
 * Before v6.4.32 the chat dumped `{"result":{"success":true,"content":"..."}}`
 * — which can be 20KB of HTML — directly into the chat transcript.
 */
import { useState } from 'react';

export interface ToolCallTrace {
    id: string;
    name: string;                   // 'file_ops' / 'browser' / 'shell' / ...
    argsPreview?: string;           // truncated raw args (≤ 300 chars)
    args?: string;                  // full args JSON string (for expand)
    success?: boolean;              // set after result arrives
    preview?: string;               // 1-line result summary
    resultTruncated?: string;       // ≤ 2KB of result JSON
    pending?: boolean;              // true between start and result
}

function parseArgs(args: string | undefined): Record<string, unknown> | null {
    if (!args) return null;
    try { return JSON.parse(args); } catch { return null; }
}

function shortLabel(call: ToolCallTrace): string {
    const parsed = parseArgs(call.args) || parseArgs(call.argsPreview) || {};
    const action = (parsed['action'] as string) || '';
    const path = (parsed['path'] as string) || (parsed['url'] as string) || '';
    const pathShort = path ? path.split('/').slice(-2).join('/') : '';
    const parts: string[] = [call.name];
    if (action) parts.push(action);
    if (pathShort) parts.push(pathShort);
    return parts.join(' · ');
}

export default function ToolCallBlock({ call }: { call: ToolCallTrace }) {
    const [expanded, setExpanded] = useState(false);
    const label = shortLabel(call);
    // v6.4.55 UX: highlight write/edit success as green "code change" card
    // with prominent +N/-N badge; keep read/list/search as quiet purple.
    const parsedArgs = parseArgs(call.args) || parseArgs(call.argsPreview) || {};
    const action = String(parsedArgs['action'] || '').toLowerCase();
    const isCodeChange = call.success === true && (action === 'write' || action === 'edit');
    const icon = call.pending
        ? <span aria-hidden style={{ display: 'inline-block', width: 10, height: 10, border: '1.5px solid currentColor', borderTopColor: 'transparent', borderRadius: '50%', animation: 'toolcall-spin 0.9s linear infinite' }} />
        : call.success === true
            ? <span aria-hidden style={{ color: '#2ea043', fontSize: 11 }}>✓</span>
            : call.success === false
                ? <span aria-hidden style={{ color: '#f85149', fontSize: 11 }}>✗</span>
                : null;

    return (
        <div
            className="tool-call-block"
            style={isCodeChange ? {
                margin: '6px 0',
                padding: '6px 10px',
                borderLeft: '3px solid rgba(46,160,67,0.8)',
                background: 'rgba(46,160,67,0.10)',
                borderRadius: '6px',
                fontSize: 12,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                fontWeight: 500,
            } : {
                margin: '4px 0',
                padding: '4px 8px',
                borderLeft: '2px solid rgba(168,85,247,0.5)',
                background: 'rgba(168,85,247,0.06)',
                borderRadius: '4px',
                fontSize: 11,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            }}
        >
            <button
                type="button"
                aria-expanded={expanded}
                onClick={() => setExpanded(!expanded)}
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    width: '100%',
                    background: 'transparent',
                    border: 'none',
                    padding: 0,
                    cursor: 'pointer',
                    color: 'var(--color-muted, #a0a0a8)',
                    textAlign: 'left',
                    fontFamily: 'inherit',
                    fontSize: 'inherit',
                }}
            >
                <span aria-hidden style={{ fontSize: 10, width: 10 }}>{expanded ? '▾' : '▸'}</span>
                {icon}
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {label}
                </span>
                {call.preview && !expanded && (
                    <span style={{ color: 'var(--text3, #666)', flexShrink: 0, maxWidth: '40%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {call.preview}
                    </span>
                )}
            </button>
            {expanded && (
                <div style={{ marginTop: 6, paddingLeft: 16, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                    <div style={{ opacity: 0.7, marginBottom: 2 }}>args:</div>
                    <div style={{ background: 'rgba(0,0,0,0.25)', padding: '4px 6px', borderRadius: 3, marginBottom: 4 }}>
                        {call.args || call.argsPreview || '(none)'}
                    </div>
                    {call.resultTruncated && (
                        <>
                            <div style={{ opacity: 0.7, marginBottom: 2 }}>result:</div>
                            <div style={{ background: 'rgba(0,0,0,0.25)', padding: '4px 6px', borderRadius: 3 }}>
                                {call.resultTruncated}
                            </div>
                        </>
                    )}
                </div>
            )}
            <style>{`@keyframes toolcall-spin { to { transform: rotate(360deg); } }`}</style>
        </div>
    );
}
