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
// V4.3 PERF: Relaxed polling intervals — old 5s/15s caused excessive
// file-tree I/O and HTTP requests, contributing to CPU heat.
const WORKSPACE_TREE_POLL_MS = 15000;
const WORKSPACE_ROOTS_POLL_MS = 30000;

function normalizeFolderPath(value: string): string {
    return String(value || '').trim().replace(/\/+$/, '');
}

function folderContainsPath(folder: string, candidate: string): boolean {
    const normalizedFolder = normalizeFolderPath(folder);
    const normalizedCandidate = normalizeFolderPath(candidate);
    if (!normalizedFolder || !normalizedCandidate) return false;
    return normalizedCandidate === normalizedFolder || normalizedCandidate.startsWith(`${normalizedFolder}/`);
}

function isDocumentVisible(): boolean {
    if (typeof document === 'undefined') return true;
    return document.visibilityState === 'visible';
}

function getRootsSignature(value: WorkspaceRootsResponse | null | undefined): string {
    const folders = Array.isArray(value?.folders)
        ? value.folders
            .map((item) => [
                normalizeFolderPath(item.path),
                String(item.label || '').trim(),
                String(item.kind || '').trim(),
                item.removable ? '1' : '0',
            ].join(':'))
            .sort()
            .join('|')
        : '';
    return [
        normalizeFolderPath(String(value?.output_dir || '')),
        normalizeFolderPath(String(value?.artifact_sync_dir || '')),
        folders,
    ].join('::');
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

    // VS Code Explorer style — compact, clean, indent guides
    const extIcon = !isDir ? getExtLabel(node.ext || '') : '';
    const fileColor = !isDir ? getExtColor(node.ext || '') : '';

    return (
        <>
            <div
                onClick={() => isDir ? onToggleDir(fullPath) : onSelectFile(fullPath, node)}
                style={{
                    display: 'flex', alignItems: 'center', gap: 0,
                    height: 22, paddingRight: 8, cursor: 'pointer',
                    fontSize: 12, lineHeight: '22px',
                    color: isSelected ? '#e6edf3' : '#c9d1d9',
                    background: isSelected ? 'rgba(79,143,255,0.12)' : 'transparent',
                    userSelect: 'none',
                }}
                onMouseEnter={(e) => { if (!isSelected) (e.currentTarget as HTMLDivElement).style.background = 'rgba(255,255,255,0.04)'; }}
                onMouseLeave={(e) => { if (!isSelected) (e.currentTarget as HTMLDivElement).style.background = isSelected ? 'rgba(79,143,255,0.12)' : 'transparent'; }}
                title={fullPath}
            >
                {/* Indent guides */}
                {Array.from({ length: depth }).map((_, d) => (
                    <span key={d} style={{ width: 16, height: 22, flexShrink: 0, borderLeft: d > 0 ? '1px solid rgba(255,255,255,0.06)' : 'none' }} />
                ))}
                {/* Chevron for dirs */}
                <span style={{ width: 16, flexShrink: 0, textAlign: 'center', fontSize: 10, color: '#8b949e' }}>
                    {isDir ? (isExpanded ? '▾' : '▸') : ''}
                </span>
                {/* Icon */}
                {isDir ? (
                    <span style={{ width: 16, flexShrink: 0, textAlign: 'center', fontSize: 12 }}>
                        {isExpanded ? '📂' : '📁'}
                    </span>
                ) : (
                    <span style={{ width: 16, flexShrink: 0, textAlign: 'center', fontSize: 8, fontWeight: 700, color: fileColor || '#8b949e', letterSpacing: 0.2 }}>
                        {extIcon}
                    </span>
                )}
                {/* Name */}
                <span style={{
                    flex: 1, minWidth: 0, marginLeft: 4,
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    fontWeight: isDir ? 500 : 400,
                    fontFamily: isDir ? 'inherit' : 'var(--font-mono, monospace)',
                }}>
                    {node.name}
                </span>
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
    const rootsRequestRef = useRef<Promise<WorkspaceRootsResponse> | null>(null);
    const rootsSignatureRef = useRef('');
    const treeRequestRef = useRef<Promise<void> | null>(null);
    const treeRequestRootRef = useRef('');
    const latestTreeRequestIdRef = useRef(0);

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
        if (rootsRequestRef.current) {
            return rootsRequestRef.current;
        }
        let request: Promise<WorkspaceRootsResponse> | null = null;
        request = (async () => {
            const resp = await fetch(`${API_BASE}/api/workspace/roots`, { cache: 'no-store' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(String(data.error || `HTTP ${resp.status}`));
            }
            const nextData = data as WorkspaceRootsResponse;
            const nextSignature = getRootsSignature(nextData);
            if (nextSignature !== rootsSignatureRef.current) {
                rootsSignatureRef.current = nextSignature;
                setRootsInfo(nextData);
            }
            return nextData;
        })().finally(() => {
            if (rootsRequestRef.current === request) {
                rootsRequestRef.current = null;
            }
        });
        rootsRequestRef.current = request;
        return request;
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

    const refresh = useCallback(async (folder?: string, { silent = false }: { silent?: boolean } = {}) => {
        const root = normalizeFolderPath(folder || activeFolder);
        if (!root) {
            setTree([]);
            setTotalFiles(0);
            return;
        }
        if (treeRequestRef.current && treeRequestRootRef.current === root) {
            return treeRequestRef.current;
        }
        const requestId = latestTreeRequestIdRef.current + 1;
        latestTreeRequestIdRef.current = requestId;
        const request = (async () => {
            if (!silent) {
                setLoading(true);
                setError('');
            }
            try {
                const data = await fetchTreeData(root);
                if (latestTreeRequestIdRef.current !== requestId) return;
                setTree(data.tree || []);
                setTotalFiles(data.total_files || 0);
                setError('');
            } catch (e) {
                if (latestTreeRequestIdRef.current !== requestId) return;
                setError(String(e));
            } finally {
                if (!silent && latestTreeRequestIdRef.current === requestId) {
                    setLoading(false);
                }
            }
        })().finally(() => {
            if (treeRequestRef.current === request) {
                treeRequestRef.current = null;
                treeRequestRootRef.current = '';
            }
        });
        treeRequestRef.current = request;
        treeRequestRootRef.current = root;
        return request;
    }, [activeFolder, fetchTreeData]);

    useEffect(() => {
        if (activeFolder) void refresh(activeFolder);
    }, [activeFolder, refresh]);

    useEffect(() => {
        if (typeof window === 'undefined') return undefined;
        const interval = window.setInterval(() => {
            if (!activeFolder || !isDocumentVisible()) return;
            void refresh(activeFolder, { silent: true });
        }, WORKSPACE_TREE_POLL_MS);
        return () => window.clearInterval(interval);
    }, [activeFolder, refresh]);

    useEffect(() => {
        if (typeof window === 'undefined') return undefined;
        const interval = window.setInterval(() => {
            if (!isDocumentVisible()) return;
            void fetchRoots().catch(() => { /* ignore */ });
        }, WORKSPACE_ROOTS_POLL_MS);
        return () => window.clearInterval(interval);
    }, [fetchRoots]);

    useEffect(() => {
        if (typeof document === 'undefined') return undefined;
        const handleVisibilityChange = () => {
            if (!isDocumentVisible()) return;
            void fetchRoots().catch(() => { /* ignore */ });
            if (activeFolder) {
                void refresh(activeFolder, { silent: true });
            }
        };
        document.addEventListener('visibilitychange', handleVisibilityChange);
        return () => {
            document.removeEventListener('visibilitychange', handleVisibilityChange);
        };
    }, [activeFolder, fetchRoots, refresh]);

    useEffect(() => {
        if (typeof window === 'undefined') return undefined;
        const scheduleRefresh = (folder: string) => {
            if (refreshTimerRef.current !== null) {
                window.clearTimeout(refreshTimerRef.current);
            }
            refreshTimerRef.current = window.setTimeout(() => {
                refreshTimerRef.current = null;
                void refresh(folder, { silent: true });
            }, WORKSPACE_REFRESH_DEBOUNCE_MS);
        };
        const handleWorkspaceUpdated = (event: Event) => {
            const detail = (event as CustomEvent<WorkspaceUpdatedDetail>).detail || {};
            const currentFolder = normalizeFolderPath(activeFolder);
            if (!currentFolder) return;
            const targetDir = normalizeFolderPath(String(detail.targetDir || ''));
            const outputDirFromEvent = normalizeFolderPath(String(detail.outputDir || ''));
            const shouldRefreshRoots = Boolean(
                (!rootsInfo && (targetDir || outputDirFromEvent))
                || (targetDir && targetDir !== artifactSyncDir)
                || (outputDirFromEvent && outputDirFromEvent !== outputDir)
            );
            const files = Array.isArray(detail.files)
                ? detail.files.map((item) => String(item || '').trim()).filter(Boolean)
                : [];
            const shouldRefresh = (
                (targetDir && targetDir === currentFolder)
                || (outputDirFromEvent && outputDirFromEvent === currentFolder)
                || files.some((item) => folderContainsPath(currentFolder, item))
            );
            if (shouldRefreshRoots) {
                void fetchRoots().catch(() => { /* ignore */ });
            }
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
    }, [activeFolder, artifactSyncDir, fetchRoots, outputDir, refresh, rootsInfo, t]);

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

    // Antigravity-style handlers
    const handleNewFolder = useCallback(async () => {
        if (!activeFolder) { alert(lang === 'zh' ? '请先选择一个文件夹' : 'Select a folder first'); return; }
        const name = prompt(lang === 'zh' ? '输入文件夹名称:' : 'Enter folder name:');
        if (!name?.trim()) return;
        try {
            const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
            const res = await fetch(`${apiBase}/api/workspace/mkdir`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root: activeFolder, path: name.trim() }) });
            if (res.ok) window.dispatchEvent(new CustomEvent('evermind:workspace-updated'));
            else alert('Failed: ' + (await res.text()));
        } catch (e) { alert('Error: ' + e); }
    }, [activeFolder, lang]);

    const handleNewFile = useCallback(async () => {
        if (!activeFolder) { alert(lang === 'zh' ? '请先选择一个文件夹' : 'Select a folder first'); return; }
        const name = prompt(lang === 'zh' ? '输入文件名:' : 'Enter file name:');
        if (!name?.trim()) return;
        try {
            const apiBase = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
            const res = await fetch(`${apiBase}/api/workspace/write`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root: activeFolder, path: name.trim(), content: '' }) });
            if (res.ok) window.dispatchEvent(new CustomEvent('evermind:workspace-updated'));
            else alert('Failed: ' + (await res.text()));
        } catch (e) { alert('Error: ' + e); }
    }, [activeFolder, lang]);

    const handleCollapseAll = useCallback(() => setExpandedDirs(new Set()), []);

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden', background: '#0d1117' }}>
            {/* Explorer header — Antigravity style */}
            <div style={{ display: 'flex', alignItems: 'center', padding: '6px 12px', borderBottom: '1px solid #21262d', flexShrink: 0 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', textTransform: 'uppercase', letterSpacing: '0.5px', flex: 1 }}>Explorer</span>
                <div style={{ display: 'flex', gap: 2 }}>
                    {[
                        { icon: '📄', title: t('New File', '新建文件'), action: handleNewFile },
                        { icon: '📁', title: t('New Folder', '新建文件夹'), action: handleNewFolder },
                        { icon: '🔄', title: t('Refresh', '刷新'), action: () => window.dispatchEvent(new CustomEvent('evermind:workspace-updated')) },
                        { icon: '⊟', title: t('Collapse All', '全部折叠'), action: handleCollapseAll },
                    ].map((btn, i) => (
                        <button key={i} onClick={btn.action} title={btn.title} style={{
                            width: 22, height: 22, display: 'flex', alignItems: 'center', justifyContent: 'center',
                            fontSize: 11, background: 'transparent', border: 'none', cursor: 'pointer', borderRadius: 3,
                            color: '#8b949e', opacity: 0.7,
                        }}
                        onMouseEnter={e => (e.currentTarget.style.opacity = '1', e.currentTarget.style.background = 'rgba(255,255,255,0.06)')}
                        onMouseLeave={e => (e.currentTarget.style.opacity = '0.7', e.currentTarget.style.background = 'transparent')}
                        >{btn.icon}</button>
                    ))}
                </div>
            </div>

            {/* Workspace folder selector — compact */}
            <div style={{ padding: '4px 8px', borderBottom: '1px solid #21262d', flexShrink: 0 }}>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                    {folderEntries.map((folder) => {
                        const isActive = activeFolder === folder.path;
                        const label = folder.path.split('/').pop() || folder.path;
                        return (
                            <button key={folder.path} onClick={() => setActiveFolder(folder.path)} title={folder.path}
                                style={{
                                    fontSize: 10, padding: '2px 8px', borderRadius: 3, border: 'none', cursor: 'pointer',
                                    background: isActive ? 'rgba(88,166,255,0.15)' : 'rgba(255,255,255,0.04)',
                                    color: isActive ? '#58a6ff' : '#8b949e', fontWeight: isActive ? 600 : 400,
                                    maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                }}>
                                {label}
                            </button>
                        );
                    })}
                    <button onClick={handleAddFolder} title={t('Add folder', '添加文件夹')} style={{
                        fontSize: 10, padding: '2px 6px', borderRadius: 3, border: '1px dashed rgba(255,255,255,0.15)',
                        background: 'transparent', color: '#8b949e', cursor: 'pointer',
                    }}>+</button>
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

            {/* Search — compact Antigravity style */}
            {activeFolder && (
                <div style={{ padding: '4px 8px', borderBottom: '1px solid #21262d', flexShrink: 0 }}>
                    <input
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder={t('Search files...', '搜索文件...')}
                        style={{
                            width: '100%', padding: '4px 8px', fontSize: 11,
                            background: 'rgba(255,255,255,0.04)', border: '1px solid #30363d',
                            borderRadius: 4, color: '#e6edf3', outline: 'none',
                        }}
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
