'use client';

import { useState, useEffect, useCallback } from 'react';
import { getHealth, getOpenClawGuide } from '@/lib/api';

export interface ConnectorEvent {
    id: string;
    type: string;
    label: string;
    timestamp: number;
    detail?: string;
}

interface OpenClawPanelProps {
    open: boolean;
    onClose: () => void;
    connected: boolean;
    running: boolean;
    lang: 'en' | 'zh';
    wsUrl: string;
    /** Recent connector events pushed from useRuntimeConnection */
    events?: ConnectorEvent[];
    runtimeMode?: string;
    activeRunStatus?: string;
    activeRunId?: string;
    runtimeId?: string;
    processId?: string;
    connectedAt?: number | null;
    lastEventAt?: number | null;
    /** Manual reconnect trigger */
    onReconnect?: () => void;
}

export default function OpenClawPanel({
    open, onClose, connected, running, lang, wsUrl,
    events = [], runtimeMode, activeRunStatus, activeRunId,
    runtimeId, processId, connectedAt, lastEventAt, onReconnect,
}: OpenClawPanelProps) {
    const tr = useCallback((zh: string, en: string) => lang === 'zh' ? zh : en, [lang]);
    const [uptime, setUptime] = useState(0);
    const [guideLoading, setGuideLoading] = useState(false);
    const [healthLoading, setHealthLoading] = useState(false);
    const [guideBundle, setGuideBundle] = useState<{
        guide?: string;
        mcp_config?: Record<string, unknown>;
        ws_url?: string;
        api_base?: string;
        guide_url?: string;
        deep_links?: { open_app?: string; run_goal_template?: string };
    } | null>(null);
    const [quickActionText, setQuickActionText] = useState('');
    const [openClawPeerCount, setOpenClawPeerCount] = useState<number | null>(null);

    // Track uptime
    useEffect(() => {
        if (!open || !connected || !connectedAt) {
            setUptime(0);
            return;
        }
        const tick = () => {
            setUptime(Math.max(0, Math.floor((Date.now() - connectedAt) / 1000)));
        };
        tick();
        const timer = window.setInterval(() => {
            tick();
        }, 1000);
        return () => window.clearInterval(timer);
    }, [open, connected, connectedAt]);

    // Close on Escape
    useEffect(() => {
        if (!open) return;
        const handler = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        window.addEventListener('keydown', handler);
        return () => window.removeEventListener('keydown', handler);
    }, [open, onClose]);

    useEffect(() => {
        if (!open) return;
        let cancelled = false;
        setGuideLoading(true);
        void getOpenClawGuide()
            .then((payload) => {
                if (cancelled) return;
                setGuideBundle(payload);
            })
            .catch(() => {
                if (cancelled) return;
                setGuideBundle(null);
            })
            .finally(() => {
                if (!cancelled) setGuideLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [open]);

    useEffect(() => {
        if (!open) return;
        let cancelled = false;
        const refreshHealth = async () => {
            setHealthLoading(true);
            try {
                const payload = await getHealth();
                if (cancelled) return;
                const totalClients = Math.max(0, Number(payload.clients_connected || 0));
                const peerCount = Math.max(0, totalClients - (connected ? 1 : 0));
                setOpenClawPeerCount(peerCount);
            } catch {
                if (!cancelled) setOpenClawPeerCount(null);
            } finally {
                if (!cancelled) setHealthLoading(false);
            }
        };

        void refreshHealth();
        const timer = window.setInterval(() => {
            void refreshHealth();
        }, connected ? 2500 : 5000);

        return () => {
            cancelled = true;
            window.clearInterval(timer);
        };
    }, [open, connected]);

    const formatUptime = (s: number) => {
        const h = Math.floor(s / 3600);
        const m = Math.floor(s / 60);
        const sec = s % 60;
        if (h > 0) return `${h}h ${m % 60}m`;
        return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
    };
    const formatTimestamp = (value?: number | null) => {
        if (!value) return '—';
        return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    };
    const humanizeRunStatus = (status?: string) => {
        const normalized = String(status || '').trim().toLowerCase();
        if (!normalized) return running ? tr('执行中', 'Executing') : tr('空闲', 'Idle');
        const zhMap: Record<string, string> = {
            queued: '排队中',
            running: '执行中',
            waiting_review: '等待审核',
            waiting_selfcheck: '等待自检',
            failed: '失败',
            done: '完成',
            cancelled: '已取消',
        };
        const enMap: Record<string, string> = {
            queued: 'Queued',
            running: 'Running',
            waiting_review: 'Awaiting review',
            waiting_selfcheck: 'Awaiting self-check',
            failed: 'Failed',
            done: 'Done',
            cancelled: 'Cancelled',
        };
        return (lang === 'zh' ? zhMap : enMap)[normalized] || status || tr('空闲', 'Idle');
    };
    const connectorTypeLabel = (type: string) => {
        const normalized = String(type || '').trim().toLowerCase();
        const zhMap: Record<string, string> = {
            bridge_connected: '桥接',
            task_created: '任务',
            run_created: '运行',
            evermind_dispatch_node: '派发',
            openclaw_node_ack: '接收',
            openclaw_node_update: '节点',
            openclaw_submit_review: '审核',
            openclaw_submit_validation: '自检',
            openclaw_run_complete: '完成',
        };
        const enMap: Record<string, string> = {
            bridge_connected: 'Bridge',
            task_created: 'Task',
            run_created: 'Run',
            evermind_dispatch_node: 'Dispatch',
            openclaw_node_ack: 'Ack',
            openclaw_node_update: 'Node',
            openclaw_submit_review: 'Review',
            openclaw_submit_validation: 'Validation',
            openclaw_run_complete: 'Complete',
        };
        return (lang === 'zh' ? zhMap : enMap)[normalized] || normalized || 'event';
    };

    const desktopStatusColor = connected ? '#22c55e' : '#ef4444';
    const peerCount = Math.max(0, Number(openClawPeerCount || 0));
    const bundleReady = Boolean(guideBundle?.ws_url || guideBundle?.guide_url || guideBundle?.mcp_config);
    const statusColor = connected ? '#22c55e' : '#ef4444';
    const statusText = !connected
        ? tr('断开', 'Disconnected')
        : peerCount > 0
            ? tr('已就绪', 'Ready')
            : tr('桥接已连通', 'Bridge Ready');
    const desktopStatusText = connected
        ? tr('桌面桥接已连接', 'Desktop bridge connected')
        : tr('桌面桥接未连接', 'Desktop bridge disconnected');
    const peerStatusText = healthLoading
        ? tr('正在检测 OpenClaw 客户端...', 'Detecting OpenClaw clients...')
        : peerCount > 0
            ? tr(`已接入 ${peerCount} 个 OpenClaw 客户端`, `${peerCount} OpenClaw client(s) attached`)
            : tr('OpenClaw 客户端可选（本地模式无需接入）', 'OpenClaw client optional (not needed for local mode)');
    const bundleStatusText = guideLoading
        ? tr('接入包同步中', 'Syncing connect bundle')
        : bundleReady
            ? tr('接入包已就绪', 'Connect bundle ready')
            : tr('接入包获取失败', 'Connect bundle unavailable');
    const runtimeLabel = String(runtimeMode || 'local').trim() || 'local';
    const resolvedWsUrl = String(guideBundle?.ws_url || wsUrl || 'ws://127.0.0.1:8765/ws').trim();
    const resolvedGuideUrl = String(guideBundle?.guide_url || '').trim();
    const resolvedDeepLink = String(guideBundle?.deep_links?.run_goal_template || 'evermind://run?goal=<urlencoded-goal>').trim();
    const mcpConfig = guideBundle?.mcp_config || {
        mcpServers: {
            evermind: {
                url: resolvedWsUrl,
                transport: 'websocket',
                description: 'Evermind God Mode',
            },
        },
    };
    const mcpConfigText = JSON.stringify(mcpConfig, null, 2);
    const quickConnectPacket = [
        '# Evermind OpenClaw Connect Bundle',
        '',
        '[mcp_config]',
        mcpConfigText,
        '',
        `[guide_url] ${resolvedGuideUrl || 'http://127.0.0.1:8765/api/openclaw-guide'}`,
        `[deep_link] ${resolvedDeepLink}`,
    ].join('\n');

    const handleCopyText = useCallback(async (value: string, successText: string, fallbackText: string) => {
        try {
            await navigator.clipboard.writeText(value);
            setQuickActionText(successText);
        } catch {
            setQuickActionText(fallbackText);
        }
    }, []);

    const handleDownloadConfig = useCallback(() => {
        try {
            const blob = new Blob([mcpConfigText], { type: 'application/json' });
            const objectUrl = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            anchor.href = objectUrl;
            anchor.download = 'evermind-openclaw-mcp.json';
            anchor.click();
            URL.revokeObjectURL(objectUrl);
            setQuickActionText(tr('MCP 配置已下载。', 'MCP config downloaded.'));
        } catch {
            setQuickActionText(tr('下载失败，请改用复制。', 'Download failed. Use copy instead.'));
        }
    }, [mcpConfigText, tr]);

    if (!open) return null;

    // Extract host from wsUrl
    const wsHost = (() => {
        try { return new URL(resolvedWsUrl.replace('ws://', 'http://').replace('wss://', 'https://')).host; }
        catch { return resolvedWsUrl; }
    })();

    const recentEvents = events.slice(-20).reverse();

    const eventTypeColor: Record<string, string> = {
        'bridge_connected': '#22c55e',
        'task_created': '#a855f7',
        'task_updated': '#a855f7',
        'task_transitioned': '#a855f7',
        'run_created': '#3b82f6',
        'run_updated': '#3b82f6',
        'run_transitioned': '#3b82f6',
        'evermind_dispatch_node': '#3b82f6',
        'openclaw_node_ack': '#f59e0b',
        'openclaw_node_update': '#f59e0b',
        'openclaw_run_complete': '#22c55e',
        'openclaw_submit_review': '#06b6d4',
        'openclaw_submit_validation': '#06b6d4',
    };

    return (
        <>
            {/* Backdrop */}
            <div
                onClick={onClose}
                style={{
                    position: 'fixed', inset: 0,
                    background: 'rgba(0,0,0,0.3)',
                    zIndex: 900,
                    transition: 'opacity 0.2s',
                }}
            />
            {/* Panel */}
            <div
                style={{
                    position: 'fixed',
                    top: 0, right: 0, bottom: 0,
                    width: 360,
                    background: 'var(--bg1, #0f1117)',
                    borderLeft: '1px solid var(--glass-border, rgba(255,255,255,0.06))',
                    zIndex: 901,
                    display: 'flex',
                    flexDirection: 'column',
                    boxShadow: '-8px 0 32px rgba(0,0,0,0.4)',
                    animation: 'ocSlideIn 0.2s ease-out',
                    fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro", "Inter", sans-serif',
                    color: 'var(--text2, #c9c9c9)',
                    fontSize: 12,
                }}
            >
                {/* Header */}
                <div style={{
                    padding: '16px 20px',
                    borderBottom: '1px solid var(--glass-border)',
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{
                            width: 9, height: 9, borderRadius: '50%',
                            background: statusColor,
                            boxShadow: connected ? `0 0 8px ${statusColor}` : 'none',
                            animation: connected ? 'ocPulse 2s ease-in-out infinite' : 'none',
                        }} />
                        <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--text1, #fff)' }}>
                            OpenClaw
                        </span>
                        <span style={{ fontSize: 11, color: statusColor, fontWeight: 600 }}>
                            {statusText}
                        </span>
                    </div>
                    <button
                        onClick={onClose}
                        style={{
                            background: 'none', border: 'none', color: 'var(--text3)', cursor: 'pointer',
                            fontSize: 16, padding: '2px 6px', borderRadius: 4,
                        }}
                        onMouseEnter={e => (e.currentTarget.style.color = 'var(--text1)')}
                        onMouseLeave={e => (e.currentTarget.style.color = 'var(--text3)')}
                    >
                        ✕
                    </button>
                </div>

                {/* Content */}
                <div style={{ flex: 1, overflowY: 'auto', padding: '0 20px' }}>
                    {/* Connection Section */}
                    <Section title={tr('连接', 'Connection')}>
                        <InfoRow label={tr('服务端', 'Endpoint')} value={wsHost} />
                        <InfoRow label={tr('总状态', 'Overall')} value={statusText} valueColor={statusColor} />
                        <InfoRow label={tr('桌面桥接', 'Desktop Bridge')} value={desktopStatusText} valueColor={desktopStatusColor} />
                        <InfoRow
                            label={tr('OpenClaw 客户端', 'OpenClaw Client')}
                            value={peerStatusText}
                            valueColor={peerCount > 0 ? '#22c55e' : (healthLoading ? '#93c5fd' : '#f59e0b')}
                        />
                        <InfoRow
                            label={tr('接入包', 'Connect Bundle')}
                            value={bundleStatusText}
                            valueColor={bundleReady ? '#22c55e' : (guideLoading ? '#93c5fd' : '#ef4444')}
                        />
                        <InfoRow label={tr('运行时', 'Runtime')} value={runtimeLabel} />
                        <InfoRow label={tr('会话 ID', 'Runtime ID')} value={runtimeId || '—'} />
                        <InfoRow label={tr('进程', 'Process')} value={processId ? `pid ${processId}` : '—'} />
                        <InfoRow label={tr('连接时间', 'Connected At')} value={formatTimestamp(connectedAt)} />
                        <InfoRow label={tr('最后事件', 'Last Event')} value={formatTimestamp(lastEventAt)} />
                        {connected && (
                            <InfoRow label={tr('在线时长', 'Uptime')} value={formatUptime(uptime)} />
                        )}
                    </Section>

                    {connected && peerCount === 0 && (
                        <div style={{
                            margin: '4px 0 14px',
                            padding: '10px 12px',
                            borderRadius: 10,
                            background: 'rgba(245, 158, 11, 0.08)',
                            border: '1px solid rgba(245, 158, 11, 0.18)',
                            color: '#fcd34d',
                            fontSize: 11,
                            lineHeight: 1.55,
                        }}>
                            {tr(
                                'Evermind 桌面桥接已经连通。现在还差一步：把下面的一键接入包或 MCP JSON 导入 OpenClaw / Claude Desktop / Cursor。导入并连接后，这里会自动变成绿色已接入。',
                                'The Evermind desktop bridge is already online. One more step is required: import the connect bundle or MCP JSON below into OpenClaw / Claude Desktop / Cursor. This panel will turn green automatically once the runtime client attaches.'
                            )}
                        </div>
                    )}

                    {/* Session Section */}
                    <Section title={tr('会话', 'Session')}>
                        <InfoRow
                            label={tr('运行状态', 'Run Status')}
                            value={humanizeRunStatus(activeRunStatus)}
                            valueColor={['running', 'queued', 'waiting_review', 'waiting_selfcheck'].includes(String(activeRunStatus || '').trim().toLowerCase()) ? '#3b82f6' : undefined}
                        />
                        <InfoRow label={tr('当前 Run', 'Active Run')} value={activeRunId ? activeRunId.slice(0, 12) : '—'} />
                        <InfoRow label={tr('事件数', 'Events')} value={String(events.length)} />
                    </Section>

                    {/* Reconnect */}
                    {!connected && onReconnect && (
                        <div style={{ padding: '12px 0' }}>
                            <button
                                onClick={onReconnect}
                                style={{
                                    width: '100%', padding: '8px 16px',
                                    background: 'rgba(99, 102, 241, 0.15)',
                                    border: '1px solid rgba(99, 102, 241, 0.3)',
                                    borderRadius: 8, color: '#818cf8', fontWeight: 600,
                                    fontSize: 12, cursor: 'pointer',
                                    transition: 'background 0.15s',
                                }}
                                onMouseEnter={e => (e.currentTarget.style.background = 'rgba(99, 102, 241, 0.25)')}
                                onMouseLeave={e => (e.currentTarget.style.background = 'rgba(99, 102, 241, 0.15)')}
                            >
                                {tr('重新连接', 'Reconnect')}
                            </button>
                        </div>
                    )}

                    {/* Recent Events */}
                    <Section title={tr('最近事件', 'Recent Events')}>
                        {recentEvents.length === 0 ? (
                            <div style={{ color: 'var(--text4)', fontSize: 11, padding: '8px 0' }}>
                                {tr('暂无事件', 'No events yet')}
                            </div>
                        ) : (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                                {recentEvents.map(evt => (
                                    <div
                                        key={evt.id}
                                        style={{
                                            display: 'flex', alignItems: 'flex-start', gap: 8,
                                            padding: '6px 0',
                                            borderBottom: '1px solid rgba(255,255,255,0.03)',
                                        }}
                                    >
                                        <span style={{
                                            width: 3, minHeight: 12, borderRadius: 2,
                                            background: eventTypeColor[evt.type] || '#6b7280',
                                            flexShrink: 0, marginTop: 2,
                                        }} />
                                        <div style={{ flex: 1, minWidth: 0 }}>
                                            <div style={{
                                                fontSize: 10, fontWeight: 600,
                                                color: eventTypeColor[evt.type] || 'var(--text3)',
                                            }}>
                                                {connectorTypeLabel(evt.type)}
                                            </div>
                                            <div style={{
                                                fontSize: 11, color: 'var(--text2)',
                                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                            }}>
                                                {evt.label}
                                            </div>
                                            {evt.detail && (
                                                <div style={{ fontSize: 10, color: 'var(--text4)', marginTop: 1 }}>
                                                    {evt.detail}
                                                </div>
                                            )}
                                        </div>
                                        <span style={{ fontSize: 9, color: 'var(--text4)', whiteSpace: 'nowrap', flexShrink: 0 }}>
                                            {new Date(evt.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                                        </span>
                                    </div>
                                ))}
                            </div>
                        )}
                    </Section>
                </div>

                {/* ── One-Click Connect 一键接入 ── */}
                <Section title={tr('🔌 一键接入 OpenClaw', '🔌 Quick Connect to OpenClaw')}>
                    <div style={{ fontSize: 11, color: 'var(--text3)', lineHeight: 1.6, marginBottom: 8 }}>
                        {tr(
                            '这里不再把长指南铺在界面上，但会从后端拉取 OpenClaw 接入包。你可以一键复制接入包、下载 MCP JSON，或复制 deep link 模板。',
                            'The long guide stays off-screen, but the panel pulls a live OpenClaw bundle from the backend. You can copy the bundle, download the MCP JSON, or copy the deep-link template.'
                        )}
                    </div>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
                        <button
                            onClick={() => void handleCopyText(
                                quickConnectPacket,
                                tr('OpenClaw 接入包已复制。', 'OpenClaw connection bundle copied.'),
                                tr('复制失败，请手动复制。', 'Copy failed. Please copy manually.'),
                            )}
                            style={quickActionButtonStyle('#60a5fa')}
                        >
                            {tr('一键复制接入包', 'Copy Connect Bundle')}
                        </button>
                        <button
                            onClick={handleDownloadConfig}
                            style={quickActionButtonStyle('#34d399')}
                        >
                            {tr('下载 MCP JSON', 'Download MCP JSON')}
                        </button>
                        <button
                            onClick={() => void handleCopyText(
                                resolvedDeepLink,
                                tr('Deep Link 模板已复制。', 'Deep-link template copied.'),
                                tr('复制失败，请手动复制。', 'Copy failed. Please copy manually.'),
                            )}
                            style={quickActionButtonStyle('#c084fc')}
                        >
                            {tr('复制 Deep Link', 'Copy Deep Link')}
                        </button>
                    </div>
                    {quickActionText && (
                        <div style={{
                            fontSize: 10,
                            color: 'var(--text3)',
                            marginBottom: 8,
                            padding: '7px 9px',
                            borderRadius: 8,
                            background: 'rgba(255,255,255,0.035)',
                            border: '1px solid rgba(255,255,255,0.06)',
                        }}>
                            {quickActionText}
                        </div>
                    )}
                    <div style={{
                        background: 'rgba(0,0,0,0.3)', borderRadius: 8, padding: '10px 12px',
                        fontFamily: 'monospace', fontSize: 10, color: '#93c5fd',
                        lineHeight: 1.5, position: 'relative', border: '1px solid rgba(59,130,246,0.15)',
                        whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                    }}>
                        {mcpConfigText}
                        <button
                            onClick={() => {
                                void handleCopyText(
                                    mcpConfigText,
                                    tr('MCP 配置已复制。', 'MCP config copied.'),
                                    tr('复制失败，请手动复制。', 'Copy failed. Please copy manually.'),
                                );
                            }}
                            style={{
                                position: 'absolute', top: 6, right: 6,
                                padding: '3px 8px', borderRadius: 4,
                                background: 'rgba(59,130,246,0.15)', border: '1px solid rgba(59,130,246,0.3)',
                                color: '#60a5fa', fontSize: 9, cursor: 'pointer',
                                transition: 'all 0.15s',
                            }}
                            onMouseOver={e => { (e.target as HTMLElement).style.background = 'rgba(59,130,246,0.3)'; }}
                            onMouseOut={e => { (e.target as HTMLElement).style.background = 'rgba(59,130,246,0.15)'; }}
                        >
                            {tr('复制配置', 'Copy Config')}
                        </button>
                    </div>
                    <div style={{ fontSize: 10, color: 'var(--text4)', marginTop: 8, lineHeight: 1.5 }}>
                        {guideLoading
                            ? tr('正在从后端同步 OpenClaw 接入信息...', 'Syncing OpenClaw connection bundle from backend...')
                            : tr('步骤：', 'Steps: ')}
                        {!guideLoading && (
                            <>
                                <br />1. {tr('点击“一键复制接入包”或“下载 MCP JSON”', 'Click “Copy Connect Bundle” or “Download MCP JSON”')}
                                <br />2. {tr('在 OpenClaw / Claude Desktop / Cursor 的 MCP 设置中导入', 'Import it into OpenClaw / Claude Desktop / Cursor MCP settings')}
                                <br />3. {tr('如需直接唤起 Evermind，可复制下方 deep link 模板', 'If you want to launch Evermind directly, copy the deep-link template below')}
                                {resolvedGuideUrl && <><br />4. {tr('完整指南接口', 'Full guide endpoint')}: {resolvedGuideUrl}</>}
                            </>
                        )}
                        {!guideLoading && (
                            <>
                                <br />
                                <span style={{ color: '#c084fc' }}>{resolvedDeepLink}</span>
                            </>
                        )}
                    </div>
                </Section>

                {/* Footer */}
                <div style={{
                    padding: '12px 20px',
                    borderTop: '1px solid var(--glass-border)',
                    fontSize: 10, color: 'var(--text4)',
                    textAlign: 'center',
                }}>
                    Evermind Desktop · OpenClaw Interface v2
                </div>
            </div>

            <style>{`
                @keyframes ocSlideIn {
                    from { transform: translateX(100%); opacity: 0; }
                    to { transform: translateX(0); opacity: 1; }
                }
                @keyframes ocPulse {
                    0%, 100% { opacity: 1; box-shadow: 0 0 8px #22c55e; }
                    50% { opacity: 0.6; box-shadow: 0 0 14px #22c55e; }
                }
            `}</style>
        </>
    );
}

/* ── Sub-components ── */

function Section({ title, children }: { title: string; children: React.ReactNode }) {
    return (
        <div style={{ padding: '14px 0', borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
            <div style={{
                fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                color: 'var(--text4)', letterSpacing: 1.5, marginBottom: 10,
            }}>
                {title}
            </div>
            {children}
        </div>
    );
}

function InfoRow({ label, value, valueColor }: { label: string; value: string; valueColor?: string }) {
    return (
        <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '3px 0', fontSize: 12,
        }}>
            <span style={{ color: 'var(--text3)' }}>{label}</span>
            <span style={{
                color: valueColor || 'var(--text2)',
                fontWeight: valueColor ? 600 : 400,
                fontFamily: 'monospace', fontSize: 11,
            }}>
                {value}
            </span>
        </div>
    );
}

function quickActionButtonStyle(color: string) {
    return {
        padding: '7px 10px',
        borderRadius: 8,
        background: `${color}20`,
        border: `1px solid ${color}4d`,
        color,
        fontSize: 11,
        fontWeight: 600,
        cursor: 'pointer',
        transition: 'background 0.15s ease',
    } as const;
}
