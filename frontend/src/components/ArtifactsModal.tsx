'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';

interface ArtifactFileItem {
    name: string;
    size: number;
}

interface ArtifactTaskItem {
    task_id: string;
    files: ArtifactFileItem[];
    html_file: string | null;
    preview_url: string | null;
}

interface ArtifactsResponse {
    tasks: ArtifactTaskItem[];
    output_dir: string;
}

interface ArtifactsModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
}

function formatSize(bytes: number): string {
    if (!Number.isFinite(bytes) || bytes < 0) return '-';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export default function ArtifactsModal({ open, onClose, lang }: ArtifactsModalProps) {
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [data, setData] = useState<ArtifactsResponse | null>(null);
    const [activeTaskId, setActiveTaskId] = useState<string>('');

    const t = useCallback((en: string, zh: string) => (lang === 'zh' ? zh : en), [lang]);

    const refresh = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const resp = await fetch(`${API_BASE}/api/preview/list`, { cache: 'no-store' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const json = (await resp.json()) as ArtifactsResponse;
            setData(json);
            if (!activeTaskId && json.tasks.length > 0) {
                setActiveTaskId(json.tasks[0].task_id);
            }
        } catch (e) {
            setError(`${t('Failed to load artifacts', '加载产物失败')}: ${e}`);
        } finally {
            setLoading(false);
        }
    }, [activeTaskId, t]);

    useEffect(() => {
        if (!open) return;
        void refresh();
    }, [open, refresh]);

    const activeTask = useMemo(() => {
        if (!data?.tasks?.length) return null;
        return data.tasks.find((item) => item.task_id === activeTaskId) || data.tasks[0];
    }, [data?.tasks, activeTaskId]);

    if (!open) return null;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-container modal-wide" onClick={(e) => e.stopPropagation()}>
                <div className="modal-header">
                    <h3>{t('Artifacts Center', '文件产物中心')}</h3>
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button className="btn text-[11px]" onClick={() => void refresh()}>
                            {t('Refresh', '刷新')}
                        </button>
                        <button className="modal-close" onClick={onClose}>✕</button>
                    </div>
                </div>

                <div className="modal-body" style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gap: 12, minHeight: 420 }}>
                    <div className="glass" style={{ borderRadius: 10, padding: 10, overflow: 'auto' }}>
                        <div style={{ fontSize: 11, color: 'var(--text3)', marginBottom: 8 }}>
                            {t('Run Directories', '任务目录')}
                        </div>
                        {loading && !data && (
                            <div style={{ fontSize: 11, color: 'var(--text2)' }}>{t('Loading...', '加载中...')}</div>
                        )}
                        {data?.tasks?.length ? data.tasks.map((task) => {
                            const active = activeTask?.task_id === task.task_id;
                            return (
                                <button
                                    key={task.task_id}
                                    className="btn"
                                    onClick={() => setActiveTaskId(task.task_id)}
                                    style={{
                                        width: '100%',
                                        justifyContent: 'space-between',
                                        marginBottom: 6,
                                        borderColor: active ? 'var(--blue)' : 'var(--glass-border)',
                                        color: active ? 'var(--blue)' : 'var(--text2)',
                                        background: active ? 'rgba(79,143,255,0.08)' : undefined,
                                    }}
                                >
                                    <span>{task.task_id}</span>
                                    <span style={{ fontSize: 10 }}>{task.files.length}</span>
                                </button>
                            );
                        }) : (
                            !loading && <div style={{ fontSize: 11, color: 'var(--text3)' }}>{t('No artifacts yet', '暂无产物')}</div>
                        )}
                    </div>

                    <div className="glass" style={{ borderRadius: 10, padding: 12, overflow: 'auto' }}>
                        {error && (
                            <div style={{ color: 'var(--red)', fontSize: 11, marginBottom: 10 }}>{error}</div>
                        )}
                        {!activeTask ? (
                            <div style={{ fontSize: 12, color: 'var(--text3)' }}>
                                {t('Select a task folder to inspect generated files.', '请选择左侧任务目录查看生成文件。')}
                            </div>
                        ) : (
                            <>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                                    <div style={{ fontSize: 13, fontWeight: 700 }}>{activeTask.task_id}</div>
                                    {activeTask.preview_url && (
                                        <button
                                            className="btn btn-primary text-[10px]"
                                            onClick={() => window.open(`${API_BASE}${activeTask.preview_url}`, '_blank', 'noopener,noreferrer')}
                                        >
                                            {t('Open Preview', '打开预览')}
                                        </button>
                                    )}
                                </div>
                                <div style={{ fontSize: 10, color: 'var(--text3)', marginBottom: 8 }}>
                                    {t('Output Dir', '输出目录')}: <code>{data?.output_dir || '-'}</code>
                                </div>

                                <div style={{ border: '1px solid var(--glass-border)', borderRadius: 8, overflow: 'hidden' }}>
                                    <div style={{
                                        display: 'grid',
                                        gridTemplateColumns: '1fr 110px 140px',
                                        padding: '8px 10px',
                                        fontSize: 10,
                                        color: 'var(--text3)',
                                        background: 'rgba(255,255,255,0.02)',
                                    }}>
                                        <div>{t('File', '文件')}</div>
                                        <div>{t('Size', '大小')}</div>
                                        <div>{t('Actions', '操作')}</div>
                                    </div>
                                    {activeTask.files.map((file) => {
                                        const isRoot = activeTask.task_id === 'root';
                                        const rawUrl = isRoot
                                            ? `${API_BASE}/preview/${encodeURIComponent(file.name)}`
                                            : `${API_BASE}/preview/${activeTask.task_id}/${encodeURIComponent(file.name)}`;
                                        const previewUrl = file.name.toLowerCase().endsWith('.html') ? rawUrl : '';
                                        return (
                                            <div
                                                key={`${activeTask.task_id}:${file.name}`}
                                                style={{
                                                    display: 'grid',
                                                    gridTemplateColumns: '1fr 110px 140px',
                                                    padding: '8px 10px',
                                                    fontSize: 11,
                                                    borderTop: '1px solid rgba(255,255,255,0.05)',
                                                    alignItems: 'center',
                                                }}
                                            >
                                                <div style={{ color: 'var(--text1)', wordBreak: 'break-all' }}>{file.name}</div>
                                                <div style={{ color: 'var(--text3)' }}>{formatSize(file.size)}</div>
                                                <div style={{ display: 'flex', gap: 6 }}>
                                                    {previewUrl ? (
                                                        <button
                                                            className="btn text-[10px]"
                                                            onClick={() => window.open(previewUrl, '_blank', 'noopener,noreferrer')}
                                                        >
                                                            {t('Preview', '预览')}
                                                        </button>
                                                    ) : (
                                                        <button
                                                            className="btn text-[10px]"
                                                            onClick={() => window.open(rawUrl, '_blank', 'noopener,noreferrer')}
                                                        >
                                                            {t('Open', '打开')}
                                                        </button>
                                                    )}
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            </>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
