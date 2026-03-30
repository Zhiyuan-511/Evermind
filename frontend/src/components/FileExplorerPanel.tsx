'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
const WORKSPACE_STORAGE_KEY = 'evermind-workspace-folders';

interface TreeNode {
    name: string;
    type: 'file' | 'directory';
    size?: number;
    ext?: string;
    mtime?: number;
    children?: TreeNode[];
}

interface FileExplorerPanelProps {
    lang: 'en' | 'zh';
    onOpenFile?: (filePath: string, root: string, content: string, ext: string) => void;
}

interface DesktopApi {
    revealInFinder?: (targetPath: string) => Promise<boolean> | boolean;
    pickFolder?: (defaultPath?: string) => Promise<string> | string;
}

interface WorkspaceRootEntry {
    path: string;
    label: string;
    kind: 'runtime_output' | 'artifact_sync' | string;
    removable: boolean;
}

interface WorkspaceRootsResponse {
    output_dir?: string;
    output_alias?: string;
    workspace?: string;
    artifact_sync_dir?: string;
    folders?: WorkspaceRootEntry[];
}

interface FolderEntry {
    path: string;
    label: string;
    kind: 'runtime_output' | 'artifact_sync' | 'custom';
    removable: boolean;
}

interface WorkspaceUpdatedDetail {
    eventType?: string;
    stage?: string;
    outputDir?: string;
    targetDir?: string;
    files?: string[];
    copiedFiles?: number;
    live?: boolean;
    final?: boolean;
    previewUrl?: string;
}

const EXT_COLORS: Record<string, string> = {
    '.html': '#e44d26', '.htm': '#e44d26',
    '.css': '#264de4',
    '.js': '#f7df1e', '.ts': '#3178c6', '.jsx': '#61dafb', '.tsx': '#3178c6',
    '.json': '#a8b9cc',
    '.md': '#ffffff', '.txt': '#999999',
    '.py': '#3572a5',
    '.svg': '#ffb13b', '.png': '#a855f7', '.jpg': '#a855f7', '.jpeg': '#a855f7', '.gif': '#a855f7', '.webp': '#a855f7',
    '.yaml': '#cb171e', '.yml': '#cb171e', '.toml': '#9c4221',
    '.sh': '#4eaa25', '.bat': '#4eaa25',
    '.log': '#888888',
    '.sql': '#e38c00',
};
const DEFAULT_FILE_COLOR = '#6b7280';
const WORKSPACE_REFRESH_DEBOUNCE_MS = 80;
const WORKSPACE_POLL_MS = 1500;

function normalizeFolderPath(value: string): string {
    return String(value || '').trim().replace(/\/+$/, '');
}

function folderContainsPath(folder: string, candidate: string): boolean {
    const normalizedFolder = normalizeFolderPath(folder);
    const normalizedCandidate = normalizeFolderPath(candidate);
    if (!normalizedFolder || !normalizedCandidate) return false;
    return normalizedCandidate === normalizedFolder || normalizedCandidate.startsWith(`${normalizedFolder}/`);
}

function formatSize(bytes: number): string {
    if (!Number.isFinite(bytes) || bytes < 0) return '-';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function getExtColor(ext: string): string {
    return EXT_COLORS[ext.toLowerCase()] || DEFAULT_FILE_COLOR;
}

function getExtLabel(ext: string): string {
    return ext.replace('.', '').toUpperCase().slice(0, 3) || '?';
}

function TreeItem({
    node,
    depth,
    path,
    expandedDirs,
    selectedPath,
    onToggleDir,
    onSelectFile,
    lang,
}: {
    node: TreeNode;
    depth: number;
    path: string;
    expandedDirs: Set<string>;
    selectedPath: string;
    onToggleDir: (path: string) => void;
    onSelectFile: (path: string, node: TreeNode) => void;
    lang: 'en' | 'zh';
}) {
    const fullPath = path ? `${path}/${node.name}` : node.name;
    const isDir = node.type === 'directory';
    const isExpanded = expandedDirs.has(fullPath);
    const isSelected = selectedPath === fullPath;
    const paddingLeft = 10 + depth * 16;
    const childCount = isDir ? (node.children?.length || 0) : 0;
    const color = isDir ? '#4f8fff' : getExtColor(node.ext || '');

    return (
        <>
            <div
                onClick={() => isDir ? onToggleDir(fullPath) : onSelectFile(fullPath, node)}
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    paddingLeft,
                    paddingRight: 8,
                    paddingTop: 5,
                    paddingBottom: 5,
                    cursor: 'pointer',
                    fontSize: 11,
                    color: isSelected ? 'var(--blue)' : 'var(--text1)',
                    background: isSelected ? 'rgba(79,143,255,0.10)' : 'transparent',
                    borderRadius: 6,
                    transition: 'background 0.12s',
                    userSelect: 'none',
                    margin: '1px 4px',
                }}
                onMouseEnter={(e) => {
                    if (!isSelected) (e.currentTarget as HTMLDivElement).style.background = 'rgba(255,255,255,0.04)';
                }}
                onMouseLeave={(e) => {
                    if (!isSelected) (e.currentTarget as HTMLDivElement).style.background = 'transparent';
                }}
                title={fullPath}
            >
                <div style={{
                    width: 24, height: 24, borderRadius: 6, flexShrink: 0,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    background: `linear-gradient(135deg, ${color}40, ${color}20)`,
                }}>
                    {isDir ? (
                        <span style={{ fontSize: 9, fontWeight: 800, color }}>{isExpanded ? '▾' : '▸'}</span>
                    ) : (
                        <span style={{ fontSize: 7, fontWeight: 800, color, letterSpacing: 0.3 }}>{getExtLabel(node.ext || '')}</span>
                    )}
                </div>

                <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{
                        fontSize: 11,
                        fontWeight: isDir ? 600 : 500,
                        whiteSpace: 'nowrap',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        lineHeight: 1.3,
                    }}>
                        {node.name}
                    </div>
                    {isDir && childCount > 0 && (
                        <div style={{ fontSize: 8, color: 'var(--text4)', lineHeight: 1.2 }}>
                            {childCount} {lang === 'zh' ? '项' : 'items'}
                        </div>
                    )}
                    {!isDir && node.size !== undefined && (
                        <div style={{ fontSize: 8, color: 'var(--text4)', lineHeight: 1.2 }}>
                            {formatSize(node.size)}
                        </div>
                    )}
                </div>

                <span style={{
                    width: 7, height: 7, borderRadius: '50%',
                    background: color, flexShrink: 0, opacity: 0.8,
                }} />
            </div>

            {isDir && isExpanded && node.children?.map((child) => (
                <TreeItem
                    key={`${fullPath}/${child.name}`}
                    node={child}
                    depth={depth + 1}
                    path={fullPath}
                    expandedDirs={expandedDirs}
                    selectedPath={selectedPath}
                    onToggleDir={onToggleDir}
                    onSelectFile={onSelectFile}
                    lang={lang}
                />
            ))}
        </>
    );
}

export default function FileExplorerPanel({ lang, onOpenFile }: FileExplorerPanelProps) {
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [tree, setTree] = useState<TreeNode[]>([]);
    const [totalFiles, setTotalFiles] = useState(0);
    const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set());
    const [selectedPath, setSelectedPath] = useState('');
    const [search, setSearch] = useState('');
    const [rootsInfo, setRootsInfo] = useState<WorkspaceRootsResponse | null>(null);

    const [customFolders, setCustomFolders] = useState<string[]>(() => {
        if (typeof window === 'undefined') return [];
        try {
            const saved = window.localStorage.getItem(WORKSPACE_STORAGE_KEY);
            const parsed = saved ? JSON.parse(saved) : [];
            return Array.isArray(parsed) ? parsed.map((item) => normalizeFolderPath(String(item))) : [];
        } catch {
            return [];
        }
    });
    const [activeFolder, setActiveFolder] = useState('');
    const refreshTimerRef = useRef<number | null>(null);

    const t = useCallback((en: string, zh: string) => (lang === 'zh' ? zh : en), [lang]);

    useEffect(() => {
        try {
            window.localStorage.setItem(WORKSPACE_STORAGE_KEY, JSON.stringify(customFolders.filter(Boolean)));
        } catch {
            /* ignore */
        }
    }, [customFolders]);

    const getDesktopApi = useCallback((): DesktopApi | undefined => {
        if (typeof window === 'undefined') return undefined;
        return (window as Window & { evermind?: DesktopApi }).evermind;
    }, []);

    const fetchRoots = useCallback(async () => {
        const resp = await fetch(`${API_BASE}/api/workspace/roots`, { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(String(data.error || `HTTP ${resp.status}`));
        }
        setRootsInfo(data as WorkspaceRootsResponse);
        return data as WorkspaceRootsResponse;
    }, []);

    const fetchTreeData = useCallback(async (root: string) => {
        const resp = await fetch(`${API_BASE}/api/workspace/tree?root=${encodeURIComponent(root)}`, { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(String(data.error || `HTTP ${resp.status}`));
        }
        return data as { root?: string; tree?: TreeNode[]; total_files?: number };
    }, []);

    const outputDir = normalizeFolderPath(String(rootsInfo?.output_dir || ''));
    const artifactSyncDir = normalizeFolderPath(String(rootsInfo?.artifact_sync_dir || ''));

    const folderEntries = useMemo<FolderEntry[]>(() => {
        const pinned: FolderEntry[] = (rootsInfo?.folders || [])
            .map((item) => ({
                path: normalizeFolderPath(item.path),
                label: item.label,
                kind: (item.kind === 'artifact_sync' ? 'artifact_sync' : 'runtime_output') as 'artifact_sync' | 'runtime_output',
                removable: Boolean(item.removable),
            }))
            .filter((item) => item.path);
        const pinnedPaths = new Set(pinned.map((item) => item.path));
        const custom = customFolders
            .map((folder) => normalizeFolderPath(folder))
            .filter((folder) => folder && !pinnedPaths.has(folder))
            .map((folder) => ({
                path: folder,
                label: folder.split('/').filter(Boolean).pop() || folder,
                kind: 'custom' as const,
                removable: true,
            }));
        return [...pinned, ...custom];
    }, [customFolders, rootsInfo?.folders]);

    useEffect(() => {
        void fetchRoots().catch((err) => setError(String(err)));
    }, [fetchRoots]);

    useEffect(() => {
        if (activeFolder && folderEntries.some((item) => item.path === activeFolder)) return;
        const fallback = folderEntries[0]?.path || '';
        if (fallback !== activeFolder) {
            setActiveFolder(fallback);
        }
    }, [activeFolder, folderEntries]);

    const refresh = useCallback(async (folder?: string) => {
        const root = normalizeFolderPath(folder || activeFolder);
        if (!root) {
            setTree([]);
            setTotalFiles(0);
            return;
        }
        setLoading(true);
        setError('');
        try {
            const data = await fetchTreeData(root);
            setTree(data.tree || []);
            setTotalFiles(data.total_files || 0);
        } catch (e) {
            setError(String(e));
        } finally {
            setLoading(false);
        }
    }, [activeFolder, fetchTreeData]);

    useEffect(() => {
        if (activeFolder) void refresh(activeFolder);
    }, [activeFolder, refresh]);

    useEffect(() => {
        const interval = setInterval(() => {
            if (activeFolder) void refresh(activeFolder);
            void fetchRoots().catch(() => { /* ignore */ });
        }, WORKSPACE_POLL_MS);
        return () => clearInterval(interval);
    }, [activeFolder, fetchRoots, refresh]);

    useEffect(() => {
        if (typeof window === 'undefined') return undefined;
        const scheduleRefresh = (folder: string) => {
            if (refreshTimerRef.current !== null) {
                window.clearTimeout(refreshTimerRef.current);
            }
            refreshTimerRef.current = window.setTimeout(() => {
                refreshTimerRef.current = null;
                void refresh(folder);
                void fetchRoots().catch(() => { /* ignore */ });
            }, WORKSPACE_REFRESH_DEBOUNCE_MS);
        };
        const handleWorkspaceUpdated = (event: Event) => {
            const detail = (event as CustomEvent<WorkspaceUpdatedDetail>).detail || {};
            const currentFolder = normalizeFolderPath(activeFolder);
            if (!currentFolder) return;
            const targetDir = normalizeFolderPath(String(detail.targetDir || ''));
            const outputDirFromEvent = normalizeFolderPath(String(detail.outputDir || ''));
            const files = Array.isArray(detail.files)
                ? detail.files.map((item) => String(item || '').trim()).filter(Boolean)
                : [];
            const shouldRefresh = (
                (targetDir && targetDir === currentFolder)
                || (outputDirFromEvent && outputDirFromEvent === currentFolder)
                || files.some((item) => folderContainsPath(currentFolder, item))
            );
            if (!shouldRefresh) return;
            if (targetDir && targetDir === currentFolder && Number(detail.copiedFiles || 0) > 0) {
                setNotice(
                    detail.live
                        ? t(
                            `Live sync updated ${Number(detail.copiedFiles || 0)} file(s) in this folder`,
                            `实时同步已更新当前文件夹中的 ${Number(detail.copiedFiles || 0)} 个文件`,
                        )
                        : t(
                            `Synced ${Number(detail.copiedFiles || 0)} file(s) to this folder`,
                            `已同步 ${Number(detail.copiedFiles || 0)} 个文件到当前文件夹`,
                        )
                );
            }
            scheduleRefresh(currentFolder);
        };

        window.addEventListener('evermind:workspace-updated', handleWorkspaceUpdated as EventListener);
        return () => {
            window.removeEventListener('evermind:workspace-updated', handleWorkspaceUpdated as EventListener);
            if (refreshTimerRef.current !== null) {
                window.clearTimeout(refreshTimerRef.current);
                refreshTimerRef.current = null;
            }
        };
    }, [activeFolder, fetchRoots, refresh, t]);

    const handleAddFolder = useCallback(async () => {
        const desktopApi = getDesktopApi();
        let folderPath = '';

        if (desktopApi?.pickFolder) {
            try {
                folderPath = String(await desktopApi.pickFolder(activeFolder || outputDir || '')).trim();
            } catch {
                folderPath = '';
            }
        }

        if (!folderPath) {
            folderPath = prompt(
                lang === 'zh'
                    ? '请输入要添加的文件夹路径:\n例如: /Users/你的用户名/Desktop/项目名'
                    : 'Enter folder path to add:\nExample: /Users/username/Desktop/project',
                activeFolder || outputDir || ''
            )?.trim() || '';
        }

        const normalized = normalizeFolderPath(folderPath);
        if (!normalized) return;

        setLoading(true);
        setError('');
        setNotice('');
        try {
            const data = await fetchTreeData(normalized);
            const resolvedRoot = normalizeFolderPath(String(data.root || normalized));
            setTree(data.tree || []);
            setTotalFiles(data.total_files || 0);
            setSelectedPath('');
            setCustomFolders((prev) => prev.includes(resolvedRoot) ? prev : [...prev, resolvedRoot]);
            setActiveFolder(resolvedRoot);
        } catch (e) {
            setError(String(e));
        } finally {
            setLoading(false);
        }
    }, [activeFolder, fetchTreeData, getDesktopApi, lang, outputDir]);

    const handleRemoveFolder = useCallback((folder: string) => {
        setCustomFolders((prev) => {
            const next = prev.filter((item) => item !== folder);
            if (activeFolder === folder) {
                const fallback = folderEntries.find((item) => item.path !== folder)?.path || '';
                setActiveFolder(fallback);
            }
            return next;
        });
    }, [activeFolder, folderEntries]);

    const handleRevealFolder = useCallback(async () => {
        if (!activeFolder) return;
        const desktopApi = getDesktopApi();
        if (!desktopApi?.revealInFinder) return;
        try {
            await desktopApi.revealInFinder(activeFolder);
        } catch {
            /* noop */
        }
    }, [activeFolder, getDesktopApi]);

    const handleToggleDir = useCallback((path: string) => {
        setExpandedDirs((prev) => {
            const next = new Set(prev);
            if (next.has(path)) next.delete(path); else next.add(path);
            return next;
        });
    }, []);

    const handleSelectFile = useCallback(async (path: string, node: TreeNode) => {
        setSelectedPath(path);
        if (!onOpenFile || !activeFolder) return;
        try {
            const resp = await fetch(`${API_BASE}/api/workspace/file?path=${encodeURIComponent(path)}&root=${encodeURIComponent(activeFolder)}`, { cache: 'no-store' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            onOpenFile(path, activeFolder, data.content || '', data.ext || node.ext || '');
        } catch (e) {
            console.error('Failed to load file:', e);
        }
    }, [activeFolder, onOpenFile]);

    const persistDeliveryFolder = useCallback(async (folder: string) => {
        const normalized = normalizeFolderPath(folder);
        if (!normalized) return;
        const resp = await fetch(`${API_BASE}/api/settings/save`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artifact_sync_dir: normalized }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(String(data.error || `HTTP ${resp.status}`));
        }
        await fetchRoots();
    }, [fetchRoots]);

    useEffect(() => {
        const normalizedActive = normalizeFolderPath(activeFolder);
        if (!normalizedActive || normalizedActive === outputDir || normalizedActive === artifactSyncDir) return;
        let cancelled = false;
        void (async () => {
            try {
                await persistDeliveryFolder(normalizedActive);
                if (!cancelled) {
                    setNotice(t(
                        `Selected folder is now the live delivery destination: ${normalizedActive}`,
                        `当前选中的文件夹已自动设为实时交付目录：${normalizedActive}`,
                    ));
                }
            } catch (e) {
                if (!cancelled) {
                    setError(String(e));
                }
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [activeFolder, artifactSyncDir, outputDir, persistDeliveryFolder, t]);

    const syncOutput = useCallback(async (targetOverride?: string) => {
        const target = normalizeFolderPath(targetOverride || (activeFolder && activeFolder !== outputDir ? activeFolder : artifactSyncDir));
        if (!target) {
            throw new Error(t('No delivery folder configured yet', '还没有配置交付文件夹'));
        }
        const resp = await fetch(`${API_BASE}/api/workspace/sync`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ root: target }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(String(data.error || `HTTP ${resp.status}`));
        }
        setNotice(
            t(
                `Synced ${Number(data.copied_files || 0)} file(s) to ${target}`,
                `已同步 ${Number(data.copied_files || 0)} 个文件到 ${target}`
            )
        );
        if (target === activeFolder) {
            await refresh(target);
        }
        await fetchRoots();
    }, [activeFolder, artifactSyncDir, fetchRoots, outputDir, refresh, t]);

    const handleSetDeliveryFolder = useCallback(async () => {
        if (!activeFolder || activeFolder === outputDir) return;
        setError('');
        setNotice('');
        try {
            await persistDeliveryFolder(activeFolder);
            await syncOutput(activeFolder);
        } catch (e) {
            setError(String(e));
        }
    }, [activeFolder, outputDir, persistDeliveryFolder, syncOutput]);

    const handleSyncNow = useCallback(async () => {
        setError('');
        setNotice('');
        try {
            await syncOutput();
        } catch (e) {
            setError(String(e));
        }
    }, [syncOutput]);

    const filteredTree = useMemo(() => {
        if (!search.trim()) return tree;
        const q = search.toLowerCase();
        function filterNodes(nodes: TreeNode[]): TreeNode[] {
            const result: TreeNode[] = [];
            for (const node of nodes) {
                if (node.type === 'directory') {
                    const filtered = filterNodes(node.children || []);
                    if (filtered.length > 0 || node.name.toLowerCase().includes(q)) {
                        result.push({ ...node, children: filtered });
                    }
                } else if (node.name.toLowerCase().includes(q)) {
                    result.push(node);
                }
            }
            return result;
        }
        return filterNodes(tree);
    }, [tree, search]);

    const activeEntry = folderEntries.find((item) => item.path === activeFolder) || null;
    const currentSyncTarget = activeFolder && activeFolder !== outputDir ? activeFolder : artifactSyncDir;
    const showSetDeliveryButton = Boolean(activeFolder && activeFolder !== outputDir);

    const badgeForEntry = useCallback((entry: FolderEntry) => {
        if (entry.kind === 'runtime_output') return t('OUT', '输出');
        if (entry.kind === 'artifact_sync') return t('SYNC', '交付');
        return t('FOLDER', '文件夹');
    }, [t]);

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
            <div style={{ padding: '8px 10px 6px', flexShrink: 0, display: 'grid', gap: 6 }}>
                <button
                    onClick={handleAddFolder}
                    style={{
                        width: '100%',
                        display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
                        padding: '8px 12px', borderRadius: 8,
                        fontSize: 11, fontWeight: 600,
                        color: '#6c5ce7',
                        background: 'rgba(108,92,231,0.08)',
                        border: '1px dashed rgba(108,92,231,0.3)',
                        cursor: 'pointer',
                        transition: 'all 0.15s',
                    }}
                >
                    + {t('Add Folder', '添加文件夹')}
                </button>

                <div style={{ display: 'grid', gridTemplateColumns: showSetDeliveryButton ? '1fr 1fr' : '1fr', gap: 6 }}>
                    {showSetDeliveryButton && (
                        <button
                            onClick={() => void handleSetDeliveryFolder()}
                            style={{
                                padding: '7px 10px',
                                borderRadius: 8,
                                fontSize: 10,
                                fontWeight: 700,
                                color: activeFolder === artifactSyncDir ? '#16a34a' : '#f59e0b',
                                background: activeFolder === artifactSyncDir ? 'rgba(34,197,94,0.10)' : 'rgba(245,158,11,0.10)',
                                border: `1px solid ${activeFolder === artifactSyncDir ? 'rgba(34,197,94,0.25)' : 'rgba(245,158,11,0.25)'}`,
                                cursor: 'pointer',
                            }}
                            title={t('Save this folder as the final delivery destination', '把当前文件夹保存为最终交付目录')}
                        >
                            {activeFolder === artifactSyncDir ? t('Delivery Folder', '当前为交付目录') : t('Set Delivery', '设为交付目录')}
                        </button>
                    )}

                    <button
                        onClick={() => void handleSyncNow()}
                        disabled={!currentSyncTarget}
                        style={{
                            padding: '7px 10px',
                            borderRadius: 8,
                            fontSize: 10,
                            fontWeight: 700,
                            color: currentSyncTarget ? '#22c55e' : 'var(--text4)',
                            background: currentSyncTarget ? 'rgba(34,197,94,0.10)' : 'rgba(255,255,255,0.04)',
                            border: `1px solid ${currentSyncTarget ? 'rgba(34,197,94,0.22)' : 'rgba(255,255,255,0.08)'}`,
                            cursor: currentSyncTarget ? 'pointer' : 'not-allowed',
                        }}
                        title={t('Copy the latest deliverable into the configured delivery folder', '把最新成品同步到交付目录')}
                    >
                        {t('Sync Deliverable', '同步成品')}
                    </button>
                </div>
            </div>

            {folderEntries.length > 0 && (
                <div style={{ padding: '0 10px 6px', flexShrink: 0 }}>
                    {folderEntries.map((folder) => {
                        const isActive = activeFolder === folder.path;
                        return (
                            <div
                                key={folder.path}
                                style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: 8,
                                    padding: '6px 8px',
                                    marginBottom: 4,
                                    borderRadius: 8,
                                    fontSize: 10,
                                    cursor: 'pointer',
                                    fontWeight: isActive ? 700 : 500,
                                    color: isActive ? 'var(--text1)' : 'var(--text3)',
                                    background: isActive ? 'rgba(255,255,255,0.06)' : 'transparent',
                                    transition: 'all 0.12s',
                                }}
                                onClick={() => setActiveFolder(folder.path)}
                                title={folder.path}
                            >
                                <span style={{
                                    minWidth: 34,
                                    height: 16,
                                    borderRadius: 999,
                                    padding: '0 6px',
                                    display: 'inline-flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    fontSize: 8,
                                    fontWeight: 800,
                                    letterSpacing: 0.3,
                                    background: folder.kind === 'runtime_output'
                                        ? 'rgba(79,143,255,0.18)'
                                        : folder.kind === 'artifact_sync'
                                            ? 'rgba(34,197,94,0.18)'
                                            : 'rgba(255,255,255,0.08)',
                                    color: folder.kind === 'runtime_output'
                                        ? '#4f8fff'
                                        : folder.kind === 'artifact_sync'
                                            ? '#22c55e'
                                            : 'var(--text3)',
                                }}>
                                    {badgeForEntry(folder)}
                                </span>

                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <div style={{ fontSize: 10, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                        {folder.label}
                                    </div>
                                    <div style={{ fontSize: 8, color: 'var(--text4)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                        {folder.path}
                                    </div>
                                </div>

                                {folder.removable && (
                                    <button
                                        onClick={(e) => { e.stopPropagation(); handleRemoveFolder(folder.path); }}
                                        title={t('Remove', '移除')}
                                        style={{
                                            background: 'none',
                                            border: 'none',
                                            cursor: 'pointer',
                                            color: 'var(--text4)',
                                            fontSize: 10,
                                            padding: '0 2px',
                                            opacity: 0.6,
                                        }}
                                    >
                                        ✕
                                    </button>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}

            {activeFolder && (
                <div style={{ padding: '0 10px 6px', flexShrink: 0, display: 'grid', gap: 6 }}>
                    <div style={{
                        padding: '8px 10px',
                        borderRadius: 8,
                        background: activeFolder === outputDir ? 'rgba(79,143,255,0.08)' : 'rgba(255,255,255,0.04)',
                        border: `1px solid ${activeFolder === outputDir ? 'rgba(79,143,255,0.18)' : 'rgba(255,255,255,0.08)'}`,
                    }}>
                        <div style={{ fontSize: 9, fontWeight: 700, color: activeFolder === outputDir ? '#4f8fff' : 'var(--text2)', marginBottom: 3 }}>
                            {activeEntry?.kind === 'runtime_output'
                                ? t('This is the real live builder output folder', '这里就是 builder 当前真实写入的输出目录')
                                : activeEntry?.kind === 'artifact_sync'
                                    ? t('This folder mirrors the latest valid builder output in real time', '这里会实时接收 builder 最新有效产物，不需要手动同步')
                                    : t('Custom folder', '自定义文件夹')}
                        </div>
                        <div style={{ fontSize: 8, color: 'var(--text4)', lineHeight: 1.5, wordBreak: 'break-all' }}>
                            {activeFolder}
                        </div>
                    </div>

                    <input
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder={t('Search files...', '搜索文件...')}
                        className="w-full bg-white/5 border border-white/10 rounded-md px-3 py-1.5 text-[10px] text-[var(--text1)] placeholder:text-[var(--text3)] focus:outline-none focus:border-[var(--blue)] transition-colors"
                    />
                </div>
            )}

            {notice && (
                <div style={{ padding: '0 10px 6px', fontSize: 10, color: '#22c55e', flexShrink: 0 }}>
                    {notice}
                </div>
            )}
            {error && (
                <div style={{ padding: '0 10px 6px', fontSize: 10, color: '#ef4444', flexShrink: 0 }}>
                    {error}
                </div>
            )}

            <div style={{ flex: 1, overflow: 'auto', padding: '0 2px' }}>
                {!activeFolder && folderEntries.length === 0 && (
                    <div style={{ padding: '24px 16px', textAlign: 'center' }}>
                        <div style={{ fontSize: 28, marginBottom: 8 }}>📁</div>
                        <div style={{ fontSize: 11, color: 'var(--text3)', marginBottom: 12 }}>
                            {t('No folder available yet', '还没有可用的文件夹')}
                        </div>
                        <div style={{ fontSize: 9, color: 'var(--text4)', lineHeight: 1.6 }}>
                            {t(
                                'The current run output will appear here automatically. You can also add your own folders.',
                                '当前运行输出目录会自动出现在这里，你也可以额外添加自己的文件夹。'
                            )}
                        </div>
                    </div>
                )}

                {loading && tree.length === 0 && (
                    <div style={{ padding: 16, fontSize: 11, color: 'var(--text3)', textAlign: 'center' }}>
                        {t('Loading...', '加载中...')}
                    </div>
                )}

                {activeFolder && !loading && filteredTree.length === 0 && !error && (
                    <div style={{ padding: 16, fontSize: 11, color: 'var(--text3)', textAlign: 'center' }}>
                        {search ? t('No matching files', '没有匹配的文件') : t('Empty directory', '空目录')}
                    </div>
                )}

                {filteredTree.map((node) => (
                    <TreeItem
                        key={node.name}
                        node={node}
                        depth={0}
                        path=""
                        expandedDirs={expandedDirs}
                        selectedPath={selectedPath}
                        onToggleDir={handleToggleDir}
                        onSelectFile={handleSelectFile}
                        lang={lang}
                    />
                ))}
            </div>

            {activeFolder && totalFiles > 0 && (
                <div style={{
                    padding: '4px 12px',
                    flexShrink: 0,
                    borderTop: '1px solid rgba(255,255,255,0.05)',
                    fontSize: 9,
                    color: 'var(--text4)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                }}>
                    <span>{totalFiles} {t('files', '文件')}</span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        {getDesktopApi()?.revealInFinder && (
                            <button
                                onClick={() => void handleRevealFolder()}
                                style={{
                                    background: 'none',
                                    border: 'none',
                                    cursor: 'pointer',
                                    color: 'var(--text4)',
                                    fontSize: 10,
                                    padding: 0,
                                }}
                                title={t('Reveal in Finder', '在访达中显示')}
                            >
                                {t('Finder', '访达')}
                            </button>
                        )}
                        <button
                            onClick={() => void refresh()}
                            style={{
                                background: 'none',
                                border: 'none',
                                cursor: 'pointer',
                                color: 'var(--text4)',
                                fontSize: 11,
                                padding: 0,
                            }}
                            title={t('Refresh', '刷新')}
                        >
                            ↻
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
