'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8765';

interface DiagnosticsModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
}

interface DiagnosticsData {
    status: string;
    output_dir: string;
    ports: {
        backend_8765: boolean;
        frontend_3000: boolean;
    };
    api_keys: Record<string, boolean>;
    tasks: {
        count: number;
        latest_task_id: string | null;
        latest_preview_url: string | null;
    };
    runtime: {
        load_avg?: {
            '1m': number | null;
            '5m': number | null;
            '15m': number | null;
        };
        clients_connected?: number | null;
        active_tasks?: number | null;
        log_file?: string | null;
        playwright_available?: boolean | null;
        playwright_reason?: string | null;
        browser_headful?: boolean | null;
        reviewer_tester_force_headful?: boolean | null;
    };
}

interface PreviewValidationResult {
    ok: boolean;
    preview_url?: string;
    errors?: string[];
    warnings?: string[];
    checks?: {
        score?: number;
        bytes?: number;
    };
    smoke?: {
        status?: string;
        reason?: string;
        http_status?: number | null;
    };
}

interface LogsResponse {
    ok: boolean;
    log_file?: string;
    tail?: number;
    lines?: string[];
    error?: string;
}

function StatusPill({ ok, label }: { ok: boolean; label: string }) {
    return (
        <span
            style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                padding: '4px 8px',
                borderRadius: 999,
                fontSize: 10,
                border: `1px solid ${ok ? 'rgba(34,197,94,0.35)' : 'rgba(239,68,68,0.35)'}`,
                color: ok ? 'var(--green)' : 'var(--red)',
                background: ok ? 'rgba(34,197,94,0.08)' : 'rgba(239,68,68,0.08)',
            }}
        >
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: ok ? 'var(--green)' : 'var(--red)' }} />
            {label}
        </span>
    );
}

export default function DiagnosticsModal({ open, onClose, lang }: DiagnosticsModalProps) {
    const [data, setData] = useState<DiagnosticsData | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [validating, setValidating] = useState(false);
    const [validation, setValidation] = useState<PreviewValidationResult | null>(null);
    const [logLines, setLogLines] = useState<string[]>([]);
    const [logFile, setLogFile] = useState('');

    const t = useCallback((en: string, zh: string) => (lang === 'zh' ? zh : en), [lang]);

    const refresh = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const [diagResp, logsResp] = await Promise.all([
                fetch(`${API_BASE}/api/diagnostics`, { cache: 'no-store' }),
                fetch(`${API_BASE}/api/logs?tail=240`, { cache: 'no-store' }),
            ]);
            if (!diagResp.ok) {
                throw new Error(`HTTP ${diagResp.status}`);
            }
            const json = await diagResp.json();
            setData(json);
            if (logsResp.ok) {
                const logs = (await logsResp.json()) as LogsResponse;
                setLogLines(Array.isArray(logs.lines) ? logs.lines : []);
                setLogFile(String(logs.log_file || ''));
            } else {
                setLogLines([]);
                setLogFile('');
            }
        } catch (e) {
            setError(`${t('Diagnostics fetch failed', '诊断信息获取失败')}: ${e}`);
        } finally {
            setLoading(false);
        }
    }, [t]);

    const validateLatestPreview = useCallback(async () => {
        if (!data?.tasks?.latest_task_id) return;
        setValidating(true);
        setValidation(null);
        try {
            const resp = await fetch(`${API_BASE}/api/preview/validate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    task_id: data.tasks.latest_task_id,
                    run_smoke: true,
                }),
            });
            const json = await resp.json();
            setValidation(json);
        } catch (e) {
            setValidation({
                ok: false,
                errors: [`${t('Validation failed', '验收失败')}: ${e}`],
            });
        } finally {
            setValidating(false);
        }
    }, [data?.tasks?.latest_task_id, t]);

    useEffect(() => {
        if (!open) return;
        void refresh();
    }, [open, refresh]);

    const configuredKeyCount = useMemo(() => {
        if (!data?.api_keys) return 0;
        return Object.values(data.api_keys).filter(Boolean).length;
    }, [data?.api_keys]);

    if (!open) return null;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-container modal-wide" onClick={(e) => e.stopPropagation()}>
                <div className="modal-header">
                    <h3>🩺 {t('Diagnostics', '运行诊断')}</h3>
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button className="btn text-[11px]" onClick={() => void refresh()}>
                            🔄 {t('Refresh', '刷新')}
                        </button>
                        <button className="modal-close" onClick={onClose}>✕</button>
                    </div>
                </div>

                <div className="modal-body" style={{ display: 'grid', gap: 14 }}>
                    {error && (
                        <div className="glass" style={{ padding: 10, borderRadius: 10, color: 'var(--red)', fontSize: 11 }}>
                            {error}
                        </div>
                    )}

                    {!error && !data && loading && (
                        <div className="glass" style={{ padding: 10, borderRadius: 10, fontSize: 11, color: 'var(--text2)' }}>
                            {t('Loading diagnostics...', '正在加载诊断信息...')}
                        </div>
                    )}

                    {data && (
                        <>
                            <div className="glass" style={{ padding: 12, borderRadius: 10 }}>
                                <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8, color: 'var(--text1)' }}>
                                    {t('Service Status', '服务状态')}
                                </div>
                                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                                    <StatusPill ok={data.ports?.backend_8765} label={t('Backend 8765', '后端 8765')} />
                                    <StatusPill ok={data.ports?.frontend_3000} label={t('Frontend 3000', '前端 3000')} />
                                    <StatusPill ok={configuredKeyCount > 0} label={`${t('API Keys', 'API 密钥')}: ${configuredKeyCount}`} />
                                    <StatusPill
                                        ok={Boolean(data.runtime?.playwright_available)}
                                        label={t('Browser Engine', '浏览器引擎')}
                                    />
                                    <StatusPill
                                        ok={Boolean(data.runtime?.browser_headful)}
                                        label={t('Global Headful', '全局可见浏览器')}
                                    />
                                    <StatusPill
                                        ok={Boolean(data.runtime?.reviewer_tester_force_headful)}
                                        label={t('Review/Test Force Visible', '审查测试强制可见')}
                                    />
                                </div>
                                <div style={{ marginTop: 10, fontSize: 10, color: 'var(--text3)' }}>
                                    {t('Output Dir', '输出目录')}: <code>{data.output_dir}</code>
                                </div>
                                {!data.runtime?.playwright_available && (
                                    <div style={{ marginTop: 6, fontSize: 10, color: 'var(--orange)' }}>
                                        {t('Playwright unavailable', 'Playwright 不可用')}: <code>{data.runtime?.playwright_reason || '-'}</code>
                                    </div>
                                )}
                            </div>

                            <div className="glass" style={{ padding: 12, borderRadius: 10 }}>
                                <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8, color: 'var(--text1)' }}>
                                    {t('Latest Task', '最新任务')}
                                </div>
                                <div style={{ fontSize: 11, color: 'var(--text2)' }}>
                                    {t('Task Count', '任务数量')}: {data.tasks?.count ?? 0}
                                </div>
                                <div style={{ fontSize: 11, color: 'var(--text2)', marginTop: 4 }}>
                                    {t('Latest Task ID', '最新任务 ID')}: <code>{data.tasks?.latest_task_id || '-'}</code>
                                </div>
                                <div style={{ fontSize: 11, color: 'var(--text2)', marginTop: 4, wordBreak: 'break-all' }}>
                                    {t('Preview URL', '预览链接')}: <code>{data.tasks?.latest_preview_url || '-'}</code>
                                </div>
                                <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                                    <button
                                        className="btn btn-primary text-[11px]"
                                        disabled={!data.tasks?.latest_preview_url || validating}
                                        onClick={() => void validateLatestPreview()}
                                    >
                                        {validating
                                            ? `⏳ ${t('Validating...', '验收中...')}`
                                            : `✅ ${t('Validate Latest Preview', '验收最新预览')}`}
                                    </button>
                                    {data.tasks?.latest_preview_url && (
                                        <button
                                            className="btn text-[11px]"
                                            onClick={() => window.open(data.tasks.latest_preview_url || '', '_blank', 'noopener,noreferrer')}
                                        >
                                            🔗 {t('Open Preview', '打开预览')}
                                        </button>
                                    )}
                                </div>
                            </div>

                            {validation && (
                                <div className="glass" style={{ padding: 12, borderRadius: 10 }}>
                                    <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8, color: validation.ok ? 'var(--green)' : 'var(--red)' }}>
                                        {validation.ok ? `✅ ${t('Validation Passed', '验收通过')}` : `❌ ${t('Validation Failed', '验收失败')}`}
                                    </div>
                                    <div style={{ fontSize: 11, color: 'var(--text2)' }}>
                                        {t('Score', '评分')}: {(validation.checks?.score ?? '-')} ·
                                        {t(' Bytes', ' 字节')}: {(validation.checks?.bytes ?? '-')}
                                    </div>
                                    <div style={{ fontSize: 11, color: 'var(--text2)', marginTop: 4 }}>
                                        Smoke: {validation.smoke?.status || 'skipped'}
                                        {validation.smoke?.http_status ? ` (HTTP ${validation.smoke.http_status})` : ''}
                                    </div>
                                    {!!validation.errors?.length && (
                                        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--red)' }}>
                                            {validation.errors.slice(0, 4).map((e, i) => <div key={i}>• {e}</div>)}
                                        </div>
                                    )}
                                    {!!validation.warnings?.length && (
                                        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--orange)' }}>
                                            {validation.warnings.slice(0, 4).map((w, i) => <div key={i}>• {w}</div>)}
                                        </div>
                                    )}
                                </div>
                            )}

                            <div className="glass" style={{ padding: 12, borderRadius: 10 }}>
                                <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8, color: 'var(--text1)' }}>
                                    {t('Execution Logs', '执行日志')}
                                </div>
                                <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 8, wordBreak: 'break-all' }}>
                                    {t('Log File', '日志文件')}: <code>{logFile || data.runtime?.log_file || '-'}</code>
                                </div>
                                <div
                                    style={{
                                        maxHeight: 220,
                                        overflow: 'auto',
                                        background: 'rgba(0,0,0,0.25)',
                                        border: '1px solid var(--glass-border)',
                                        borderRadius: 8,
                                        padding: 8,
                                    }}
                                >
                                    <pre
                                        style={{
                                            margin: 0,
                                            fontSize: 10,
                                            lineHeight: 1.5,
                                            color: 'var(--text2)',
                                            whiteSpace: 'pre-wrap',
                                            wordBreak: 'break-word',
                                        }}
                                    >
                                        {logLines.length > 0
                                            ? logLines.join('\n')
                                            : t('No logs yet. Run a task and click refresh.', '暂无日志。先运行一次任务，再点刷新。')}
                                    </pre>
                                </div>
                            </div>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}
