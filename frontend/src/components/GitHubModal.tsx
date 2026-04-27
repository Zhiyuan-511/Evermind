'use client';

import { useEffect, useState, useCallback } from 'react';

interface GitHubModalProps {
    onClose: () => void;
    lang: 'en' | 'zh';
    apiBase?: string; // defaults to same origin, can inject for dev
}

interface GitStatus {
    authenticated: boolean;
    username?: string;
    is_repo: boolean;
    branch?: string;
    dirty_files: number;
    has_remote: boolean;
    remote_url?: string;
    project_path: string;
    project_exists: boolean;
    auth_error?: string;
}

export default function GitHubModal({ onClose, lang, apiBase = 'http://127.0.0.1:8765' }: GitHubModalProps) {
    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);
    const [status, setStatus] = useState<GitStatus | null>(null);
    const [loading, setLoading] = useState(true);
    const [banner, setBanner] = useState<{ kind: 'ok' | 'warn' | 'error'; msg: string } | null>(null);
    const [pat, setPat] = useState('');
    const [repoName, setRepoName] = useState('');
    const [isPrivate, setIsPrivate] = useState(true);
    const [commitMsg, setCommitMsg] = useState('');
    const [busy, setBusy] = useState(false);

    const api = useCallback(
        async (path: string, options: RequestInit = {}) => {
            try {
                const res = await fetch(`${apiBase}/api/git${path}`, {
                    headers: { 'Content-Type': 'application/json' },
                    ...options,
                });
                const body = await res.json().catch(() => ({}));
                return { ok: res.ok, status: res.status, body };
            } catch (e) {
                return { ok: false, status: 0, body: { detail: String(e) } };
            }
        },
        [apiBase]
    );

    const refresh = useCallback(async () => {
        setLoading(true);
        const r = await api('/status');
        if (r.ok) setStatus(r.body as GitStatus);
        setLoading(false);
    }, [api]);

    useEffect(() => { refresh(); }, [refresh]);

    const showBanner = (kind: 'ok' | 'warn' | 'error', msg: string) => {
        setBanner({ kind, msg });
        if (kind === 'ok') setTimeout(() => setBanner(null), 3500);
    };

    const savePat = async () => {
        const token = pat.trim();
        if (!token) return showBanner('error', tr('请粘贴 token', 'Token is empty'));
        setBusy(true);
        showBanner('warn', tr('验证中...', 'Verifying...'));
        const r = await api('/auth/pat', { method: 'POST', body: JSON.stringify({ token }) });
        setBusy(false);
        if (r.ok) {
            showBanner('ok', tr(`已连接 ${r.body.username}`, `Connected as ${r.body.username}`));
            setPat('');
            refresh();
        } else {
            showBanner('error', r.body.detail || `HTTP ${r.status}`);
        }
    };

    const disconnect = async () => {
        if (!confirm(tr('确认注销 GitHub token？', 'Disconnect GitHub?'))) return;
        await api('/auth', { method: 'DELETE' });
        refresh();
    };

    const publish = async () => {
        const name = repoName.trim();
        if (!name) return showBanner('error', tr('请填仓库名', 'Repo name required'));
        setBusy(true);
        showBanner('warn', tr('发布中... (init → commit → create → push)', 'Publishing...'));
        const r = await api('/publish', {
            method: 'POST',
            body: JSON.stringify({ name, private: isPrivate }),
        });
        setBusy(false);
        if (r.ok) {
            showBanner('ok', tr(`发布成功: ${r.body.repo_name}`, `Published: ${r.body.repo_name}`));
            setRepoName('');
            refresh();
        } else {
            showBanner('error', r.body.detail || `HTTP ${r.status}`);
        }
    };

    const commitPush = async () => {
        setBusy(true);
        showBanner('warn', tr('推送中...', 'Pushing...'));
        const r = await api('/commit_push', {
            method: 'POST',
            body: JSON.stringify({ message: commitMsg.trim() }),
        });
        setBusy(false);
        if (r.ok) {
            showBanner('ok', r.body.committed ? tr('已提交并推送', 'Committed + pushed') : tr('已推送', 'Pushed'));
            setCommitMsg('');
            refresh();
        } else {
            showBanner('error', r.body.detail || `HTTP ${r.status}`);
        }
    };

    const openInGitHub = () => {
        if (!status?.remote_url) return;
        const htmlUrl = status.remote_url.replace(/\.git$/, '').replace('https://x-access-token:', '').replace(/@github\.com/, '');
        window.open(htmlUrl, '_blank');
    };

    const bgOverlay: React.CSSProperties = {
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 1000,
    };
    const modalStyle: React.CSSProperties = {
        background: '#1c1c1e', color: '#f5f5f7', borderRadius: 12,
        width: 'min(640px, 92vw)', maxHeight: '85vh', overflow: 'auto',
        boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
        border: '1px solid rgba(255,255,255,0.1)',
    };
    const hdrStyle: React.CSSProperties = {
        padding: '14px 20px', borderBottom: '1px solid rgba(255,255,255,0.08)',
        display: 'flex', alignItems: 'center', gap: 8,
        fontSize: 16, fontWeight: 600,
    };
    const dotStyle = (color: string): React.CSSProperties => ({
        display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: color, marginLeft: 4,
    });
    const bodyStyle: React.CSSProperties = { padding: 20 };
    const btnPrimary: React.CSSProperties = {
        background: 'linear-gradient(135deg,#0071e3,#af52de)', color: '#fff',
        border: 'none', padding: '10px 16px', borderRadius: 6,
        cursor: busy ? 'not-allowed' : 'pointer', fontSize: 13, fontWeight: 600,
        opacity: busy ? 0.6 : 1,
    };
    const btnGhost: React.CSSProperties = {
        background: 'transparent', color: '#64d2ff',
        border: '1px solid rgba(100,210,255,0.3)', padding: '8px 14px', borderRadius: 6,
        cursor: 'pointer', fontSize: 12,
    };
    const inputStyle: React.CSSProperties = {
        width: '100%', background: '#0d1117', border: '1px solid #30363d',
        color: '#e6e6e6', padding: 8, borderRadius: 6, fontSize: 13,
        fontFamily: 'monospace',
    };
    const cardStyle: React.CSSProperties = {
        background: '#0d1117', border: '1px solid #30363d',
        borderRadius: 8, padding: 12, marginBottom: 14, fontSize: 12,
    };
    const rowStyle: React.CSSProperties = {
        display: 'flex', justifyContent: 'space-between', padding: '4px 0',
    };
    const bannerStyle = (kind: 'ok' | 'warn' | 'error'): React.CSSProperties => ({
        padding: '8px 12px', borderRadius: 6, marginBottom: 12, fontSize: 12,
        background: kind === 'error' ? '#3a0d0d' : kind === 'warn' ? '#3a2e00' : '#0d2d1a',
        borderLeft: `3px solid ${kind === 'error' ? '#f85149' : kind === 'warn' ? '#d29922' : '#3fb950'}`,
    });

    const dotColor = status?.authenticated ? '#3fb950' : '#666';
    const dotTitle = status?.authenticated
        ? `Connected${status.username ? `: ${status.username}` : ''}`
        : tr('未连接', 'Not connected');

    return (
        <div style={bgOverlay} onClick={onClose}>
            <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
                <div style={hdrStyle}>
                    <span style={{ fontSize: 20 }}>🐙</span>
                    <span>GitHub</span>
                    <span style={dotStyle(dotColor)} title={dotTitle}></span>
                    <span style={{ flex: 1 }}></span>
                    <button onClick={onClose} style={{ ...btnGhost, padding: '4px 10px', fontSize: 11 }}>
                        {tr('关闭', 'Close')}
                    </button>
                </div>
                <div style={bodyStyle}>
                    {banner && <div style={bannerStyle(banner.kind)}>{banner.msg}</div>}

                    {loading && <div style={{ textAlign: 'center', color: '#888', padding: 20 }}>
                        {tr('加载中...', 'Loading...')}
                    </div>}

                    {!loading && status && !status.authenticated && (
                        <div>
                            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 10 }}>
                                {tr('首次使用：配置 GitHub Personal Access Token', 'First-time setup: GitHub Personal Access Token')}
                            </div>
                            <ol style={{ paddingLeft: 20, lineHeight: 1.9, fontSize: 13, color: '#ccc' }}>
                                <li>
                                    {tr('打开', 'Open')}{' '}
                                    <a
                                        href="https://github.com/settings/tokens/new?scopes=repo&description=Evermind"
                                        target="_blank"
                                        rel="noreferrer"
                                        style={{ color: '#58a6ff' }}
                                    >
                                        {tr('GitHub Token 页面', 'GitHub Token page')}
                                    </a>
                                    {tr('，勾选 ', ', check ')}
                                    <code style={{ background: '#0d1117', padding: '2px 5px', borderRadius: 3 }}>repo</code>
                                    {tr(' 权限，点击 Generate token', ' scope, click Generate token')}
                                </li>
                                <li>{tr('复制生成的 token（格式 ghp_...）', 'Copy the token (starts with ghp_...)')}</li>
                                <li>
                                    {tr('粘贴到下方：', 'Paste below:')}
                                    <input
                                        type="password"
                                        value={pat}
                                        onChange={(e) => setPat(e.target.value)}
                                        placeholder="ghp_xxxxxxxxxxxxxxxx..."
                                        style={{ ...inputStyle, marginTop: 6 }}
                                    />
                                </li>
                            </ol>
                            <div style={{ textAlign: 'right', marginTop: 14 }}>
                                <button onClick={savePat} style={btnPrimary} disabled={busy}>
                                    {tr('保存并验证', 'Save & Verify')}
                                </button>
                            </div>
                        </div>
                    )}

                    {!loading && status && status.authenticated && (
                        <div>
                            <div style={cardStyle}>
                                <div style={rowStyle}>
                                    <span style={{ color: '#888' }}>{tr('用户', 'User')}</span>
                                    <code>{status.username || '?'}</code>
                                </div>
                                <div style={rowStyle}>
                                    <span style={{ color: '#888' }}>{tr('项目路径', 'Project path')}</span>
                                    <code style={{ fontSize: 11 }}>{status.project_path}</code>
                                </div>
                                <div style={rowStyle}>
                                    <span style={{ color: '#888' }}>Git</span>
                                    <span>{status.is_repo ? `${status.branch || 'main'} ${tr('分支', 'branch')}` : tr('尚未初始化', 'Not initialized')}</span>
                                </div>
                                <div style={rowStyle}>
                                    <span style={{ color: '#888' }}>{tr('远程', 'Remote')}</span>
                                    <span>
                                        {status.has_remote && status.remote_url ? (
                                            <a
                                                href={status.remote_url.replace(/\.git$/, '')}
                                                target="_blank"
                                                rel="noreferrer"
                                                style={{ color: '#58a6ff' }}
                                            >
                                                {status.remote_url.replace(/\.git$/, '').replace(/https:\/\/github\.com\//, '')}
                                            </a>
                                        ) : (
                                            <span style={{ color: '#666' }}>{tr('尚未发布', 'Not published')}</span>
                                        )}
                                    </span>
                                </div>
                                <div style={rowStyle}>
                                    <span style={{ color: '#888' }}>{tr('未提交文件', 'Dirty files')}</span>
                                    <span>{status.dirty_files} {tr('个', '')}</span>
                                </div>
                            </div>

                            {status.has_remote ? (
                                <>
                                    <textarea
                                        value={commitMsg}
                                        onChange={(e) => setCommitMsg(e.target.value)}
                                        rows={3}
                                        placeholder={tr('Commit message (留空自动生成)', 'Commit message (optional)')}
                                        style={{ ...inputStyle, fontFamily: 'inherit', resize: 'vertical', marginBottom: 12 }}
                                    />
                                    <div style={{ display: 'flex', gap: 10 }}>
                                        <button onClick={commitPush} style={{ ...btnPrimary, flex: 1 }} disabled={busy}>
                                            Commit &amp; Push
                                        </button>
                                        <button onClick={openInGitHub} style={btnGhost}>
                                            {tr('在 GitHub 查看', 'View on GitHub')}
                                        </button>
                                        <button onClick={disconnect} style={{ ...btnGhost, color: '#f85149', borderColor: 'rgba(248,81,73,0.3)' }}>
                                            {tr('注销', 'Logout')}
                                        </button>
                                    </div>
                                </>
                            ) : (
                                <div>
                                    <div style={{ border: '1px solid #30363d', padding: 14, borderRadius: 8, marginBottom: 12 }}>
                                        <div style={{ fontSize: 13, color: '#ccc', marginBottom: 10 }}>
                                            {tr('首次发布到 GitHub：', 'Publish to GitHub:')}
                                        </div>
                                        <label style={{ display: 'block', marginBottom: 8 }}>
                                            <span style={{ fontSize: 11, color: '#888', display: 'block', marginBottom: 3 }}>
                                                {tr('仓库名', 'Repo name')}
                                            </span>
                                            <input
                                                type="text"
                                                value={repoName}
                                                onChange={(e) => setRepoName(e.target.value)}
                                                placeholder="my-evermind-project"
                                                style={{ ...inputStyle, fontFamily: 'inherit' }}
                                            />
                                        </label>
                                        <label style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
                                            <input
                                                type="checkbox"
                                                checked={isPrivate}
                                                onChange={(e) => setIsPrivate(e.target.checked)}
                                                style={{ marginRight: 6 }}
                                            />
                                            Private
                                        </label>
                                        <button onClick={publish} style={{ ...btnPrimary, width: '100%' }} disabled={busy}>
                                            {tr('一键发布到 GitHub', 'Publish to GitHub')}
                                        </button>
                                    </div>
                                    <div style={{ textAlign: 'right' }}>
                                        <button onClick={disconnect} style={{ ...btnGhost, color: '#f85149', borderColor: 'rgba(248,81,73,0.3)', fontSize: 11 }}>
                                            {tr('注销 Token', 'Logout')}
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
