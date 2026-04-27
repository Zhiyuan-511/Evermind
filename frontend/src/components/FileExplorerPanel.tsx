'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ContextMenu, { type ContextMenuItem } from './ContextMenu';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
const WORKSPACE_STORAGE_KEY = 'evermind-workspace-folders';
const PINNED_PATHS_STORAGE_KEY = 'evermind-pinned-folders';

/**
 * v5.6: Recursively walk a dropped entry (from DataTransferItem.webkitGetAsEntry).
 * Flattens a folder tree into a list of { relPath, file } tuples so we can POST
 * them via FormData. Chromium caps readEntries at 100 per call → we loop until empty.
 */
async function walkFileSystemEntry(
    entry: FileSystemEntry,
    basePath = '',
): Promise<Array<{ relPath: string; file: File }>> {
    if (entry.isFile) {
        const fileEntry = entry as FileSystemFileEntry;
        return new Promise((resolve, reject) => {
            fileEntry.file(
                (file) => resolve([{ relPath: basePath + entry.name, file }]),
                (err) => reject(err),
            );
        });
    }
    if (entry.isDirectory) {
        const dirEntry = entry as FileSystemDirectoryEntry;
        const reader = dirEntry.createReader();
        const readAll = async (): Promise<FileSystemEntry[]> => {
            const all: FileSystemEntry[] = [];
            while (true) {
                const batch: FileSystemEntry[] = await new Promise((resolve, reject) => {
                    reader.readEntries(
                        (entries) => resolve(entries),
                        (err) => reject(err),
                    );
                });
                if (!batch.length) break;
                all.push(...batch);
            }
            return all;
        };
        const children = await readAll();
        const nested = await Promise.all(
            children.map((c) => walkFileSystemEntry(c, basePath + entry.name + '/')),
        );
        return nested.flat();
    }
    return [];
}

type UploadProgress = { loaded: number; total: number; currentFile: string; done: number; totalFiles: number };

/** Upload a File as base64 JSON to /api/workspace/upload.
 * Chosen over multipart/form-data to avoid a hard python-multipart dep.
 * Progress granularity = per-file (byte-level would require XHR + multipart). */
// v7.3.9 audit-fix CRITICAL — cap individual upload at 25 MB.
// Without a cap, a 50 MB+ file × chunk-base64 path triggers severe GC
// pressure (~30s main-thread freeze) and the user sees a frozen UI with no
// abort. 25 MB is generous for source code / images / docs and keeps the
// encode loop under 1.5 s.
const UPLOAD_MAX_BYTES = 25 * 1024 * 1024;

async function uploadFileBase64(
    root: string,
    relPath: string,
    file: File,
): Promise<{ ok: boolean; error?: string }> {
    if (file.size > UPLOAD_MAX_BYTES) {
        return {
            ok: false,
            error: `File too large (${Math.round(file.size / 1024 / 1024)} MB > 25 MB). Split or zip before upload.`,
        };
    }
    const buf = await file.arrayBuffer();
    // Chunked base64 encoder — avoid btoa argument-length limits.
    // v7.3.9 — use Uint8Array.subarray + tight for-loop instead of
    // Array.from(...) materializing a fresh JS array per chunk.
    const bytes = new Uint8Array(buf);
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
        const sub = bytes.subarray(i, i + chunk);
        let s = '';
        for (let j = 0; j < sub.length; j++) s += String.fromCharCode(sub[j]);
        binary += s;
    }
    const content_b64 = btoa(binary);
    try {
        const res = await fetch(`${API_BASE}/api/workspace/upload`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ root, path: relPath, content_b64 }),
        });
        if (!res.ok) return { ok: false, error: `HTTP ${res.status}: ${(await res.text()).slice(0, 200)}` };
        return { ok: true };
    } catch (e) {
        return { ok: false, error: String(e) };
    }
}

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

// Sidebar palette — "Steel Ink" (JetBrains New UI inversion strategy).
// Chrome (--bg2 #16181C) is *lighter* than the editor (--bg1 #0E1013)
// because JetBrains' research shows code reads best on the deepest surface,
// while panel UI benefits from slightly more lift. Fully cold-neutral
// (R<G<B) — no purple hangover from the prior palette.
const UI = {
    sidebarBg:       '#16181C',          // chrome (matches CSS --bg2)
    rowText:         '#E4E6EB',          // non-pure white (--text1)
    rowTextMuted:    '#9BA1AC',          // section headers (--text2)
    rowTextDim:      '#5F6572',          // placeholders / dim actions (--text3)
    rowHoverBg:      '#1E2127',          // --bg3 — gentle lift
    rowActiveBg:     '#2A3A66',          // accent-muted (--blue @ 25% on cold-black)
    rowActiveText:   '#FFFFFF',
    indentGuide:     '#32363F',          // --border-strong
    indentGuideDim:  '#23262D',          // --border
    sectionBorder:   '#23262D',
    focusRing:       '#5B8CFF',          // steel blue accent (--blue)
    sansStack:       '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif',
    rowHeight:       22,
    indentStep:      16,
    iconSize:        16,
    uiFontSize:      13,
};

// File-extension accent colors, used to tint the monochrome SVG file icon.
// Matches the Material Icon Theme palette roughly so files remain visually distinct.
const EXT_COLORS: Record<string, string> = {
    '.html': '#e44d26', '.htm': '#e44d26',
    '.css': '#42a5f5', '.scss': '#c6538c', '.less': '#1d365d',
    '.js': '#f7df1e', '.mjs': '#f7df1e', '.cjs': '#f7df1e',
    '.ts': '#3178c6', '.jsx': '#61dafb', '.tsx': '#61dafb',
    '.json': '#ffca28', '.jsonc': '#ffca28',
    '.md': '#42a5f5', '.mdx': '#42a5f5', '.txt': '#9e9e9e',
    '.py': '#4b8bbe', '.rb': '#cc342d', '.go': '#00acd7', '.rs': '#ce412b',
    '.java': '#ea2d2e', '.kt': '#a97bff', '.swift': '#f16529', '.cpp': '#5c8dbc',
    '.c': '#5c8dbc', '.h': '#a074c4', '.hpp': '#a074c4',
    '.svg': '#ffb13b', '.png': '#a855f7', '.jpg': '#a855f7', '.jpeg': '#a855f7', '.gif': '#a855f7', '.webp': '#a855f7', '.ico': '#a855f7',
    '.yaml': '#cb171e', '.yml': '#cb171e', '.toml': '#9c4221',
    '.sh': '#4eaa25', '.bash': '#4eaa25', '.zsh': '#4eaa25', '.bat': '#4eaa25', '.ps1': '#012456',
    '.log': '#888888', '.sql': '#e38c00',
    '.env': '#ecd53f', '.gitignore': '#f05033', '.gitattributes': '#f05033',
    '.lock': '#9d9d9d', '.dockerfile': '#0db7ed',
};
const DEFAULT_FILE_COLOR = '#9D9D9D';
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

// Monochrome codicon-style chevron, rendered as SVG for crisp rasterization
// across OSes (Unicode ▸/▾ renders unevenly).
function Chevron({ expanded, color = UI.rowTextMuted }: { expanded: boolean; color?: string }) {
    return (
        <svg
            width={UI.iconSize}
            height={UI.iconSize}
            viewBox="0 0 16 16"
            fill="none"
            style={{
                flexShrink: 0,
                transition: 'transform 150ms ease',
                transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)',
                color,
            }}
        >
            <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
    );
}

// Filled folder glyph, Material Icon Theme style — slightly more polished
// than a plain outline. Uses currentColor so it inherits the row tint.
function FolderGlyph({ open, color = '#DCB67A' }: { open: boolean; color?: string }) {
    return (
        <svg width={UI.iconSize} height={UI.iconSize} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
            {open ? (
                <path d="M1.5 13V4.5A1.5 1.5 0 0 1 3 3h3.17a1.5 1.5 0 0 1 1.06.44L8.5 4.5H13a1.5 1.5 0 0 1 1.5 1.5v.5H3.5L1.9 13.5A.5.5 0 0 0 2.4 14h10.1a.5.5 0 0 0 .48-.37L14.5 8"
                    stroke={color} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" fill={`${color}22`}/>
            ) : (
                <path d="M1.5 13V4.5A1.5 1.5 0 0 1 3 3h3.17a1.5 1.5 0 0 1 1.06.44L8.5 4.5H13A1.5 1.5 0 0 1 14.5 6v7A1.5 1.5 0 0 1 13 14.5H3A1.5 1.5 0 0 1 1.5 13Z"
                    stroke={color} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" fill={`${color}22`}/>
            )}
        </svg>
    );
}

// Generic file glyph — document silhouette tinted by file-extension color.
// The top-right corner fold keeps it recognizable without per-language
// custom icons, which would require a ~30-file asset pipeline.
function FileGlyph({ color }: { color: string }) {
    return (
        <svg width={UI.iconSize} height={UI.iconSize} viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
            <path d="M3.5 1.5h6l3 3V14a1 1 0 0 1-1 1h-8a1 1 0 0 1-1-1V2.5a1 1 0 0 1 1-1Z"
                stroke={color} strokeWidth="1.15" strokeLinejoin="round" fill={`${color}1c`}/>
            <path d="M9.5 1.5v3h3" stroke={color} strokeWidth="1.15" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
    );
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

    return (
        <>
            <div
                onClick={() => isDir ? onToggleDir(fullPath) : onSelectFile(fullPath, node)}
                style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    height: UI.rowHeight, paddingRight: 12, cursor: 'pointer',
                    fontSize: UI.uiFontSize, lineHeight: `${UI.rowHeight}px`,
                    color: isSelected ? UI.rowActiveText : UI.rowText,
                    background: isSelected ? UI.rowActiveBg : 'transparent',
                    userSelect: 'none',
                    fontFamily: UI.sansStack,
                }}
                onMouseEnter={(e) => { if (!isSelected) (e.currentTarget as HTMLDivElement).style.background = UI.rowHoverBg; }}
                onMouseLeave={(e) => { if (!isSelected) (e.currentTarget as HTMLDivElement).style.background = 'transparent'; }}
                title={fullPath}
            >
                {/* Indent guides — depth-wise vertical lines, brighter on active branch */}
                {Array.from({ length: depth }).map((_, d) => (
                    <span key={d} style={{
                        width: UI.indentStep, height: UI.rowHeight, flexShrink: 0,
                        borderLeft: `1px solid ${isSelected ? UI.indentGuide : UI.indentGuideDim}`,
                        marginLeft: d === 0 ? 4 : 0,
                    }} />
                ))}
                {/* Chevron slot — only dirs get a chevron; files get an empty slot for alignment */}
                <span style={{ width: UI.iconSize, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    {isDir && <Chevron expanded={isExpanded} color={isSelected ? UI.rowActiveText : UI.rowTextMuted} />}
                </span>
                {/* File/folder glyph */}
                {isDir
                    ? <FolderGlyph open={isExpanded} color={isSelected ? UI.rowActiveText : '#DCB67A'} />
                    : <FileGlyph color={getExtColor(node.ext || '')} />}
                {/* Name */}
                <span style={{
                    flex: 1, minWidth: 0,
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    fontWeight: 400,
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

    // v5.6: pinned folders — sorted first. Persisted in localStorage.
    const [pinnedPaths, setPinnedPaths] = useState<Set<string>>(() => {
        try {
            const raw = window.localStorage.getItem(PINNED_PATHS_STORAGE_KEY);
            if (!raw) return new Set();
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? new Set(parsed) : new Set();
        } catch {
            return new Set();
        }
    });
    useEffect(() => {
        try {
            window.localStorage.setItem(PINNED_PATHS_STORAGE_KEY, JSON.stringify(Array.from(pinnedPaths)));
        } catch {
            /* ignore */
        }
    }, [pinnedPaths]);
    const togglePin = useCallback((path: string) => {
        setPinnedPaths((prev) => {
            const next = new Set(prev);
            if (next.has(path)) next.delete(path);
            else next.add(path);
            return next;
        });
    }, []);

    // v5.6: context menu state.
    const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; items: ContextMenuItem[] } | null>(null);

    // v5.6: upload progress overlay state.
    const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);

    // v5.7: inline input row — Electron's renderer blocks window.prompt()/alert(),
    // so we draw our own input row + toast instead. Supports 3 modes:
    //   file   → new file inside activeFolder
    //   folder → new folder inside activeFolder
    //   addRoot → add a new workspace root (pasted absolute path)
    const [inlineAction, setInlineAction] = useState<'file' | 'folder' | 'addRoot' | null>(null);
    const [inlineName, setInlineName] = useState('');
    const [inlineError, setInlineError] = useState('');
    const inlineInputRef = useRef<HTMLInputElement | null>(null);
    useEffect(() => {
        if (inlineAction && inlineInputRef.current) {
            inlineInputRef.current.focus();
            inlineInputRef.current.select();
            // v5.8: scroll the input into view so users can see it even if the
            // panel has been scrolled. Fixes "I clicked Add but nothing happened"
            // where the input was rendered off-screen.
            inlineInputRef.current.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
    }, [inlineAction]);

    // v5.8.1: persistent "last added" state. The CTA card morphs into a
    // green folder chip once the user adds a folder, and stays that way
    // permanently — clicking it opens Add again so the user can replace it
    // with a new one. (Earlier version auto-reverted after 2.5s, which users
    // found confusing because they want to see what they added.)
    const [recentlyAddedPath, setRecentlyAddedPath] = useState<string>(() => {
        if (typeof window === 'undefined') return '';
        try { return window.localStorage.getItem('evermind-last-added-folder') || ''; } catch { return ''; }
    });
    useEffect(() => {
        try {
            if (recentlyAddedPath) {
                window.localStorage.setItem('evermind-last-added-folder', recentlyAddedPath);
            }
        } catch { /* ignore */ }
    }, [recentlyAddedPath]);

    // v5.7: lightweight toast replacing alert()
    const [toast, setToast] = useState<{ message: string; kind: 'info' | 'error' | 'success' } | null>(null);
    useEffect(() => {
        if (!toast) return;
        const handle = window.setTimeout(() => setToast(null), 3500);
        return () => window.clearTimeout(handle);
    }, [toast]);
    const showToast = useCallback((message: string, kind: 'info' | 'error' | 'success' = 'info') => {
        setToast({ message, kind });
    }, []);

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
        const systemPinned: FolderEntry[] = (rootsInfo?.folders || [])
            .map((item) => ({
                path: normalizeFolderPath(item.path),
                label: item.label,
                kind: (item.kind === 'artifact_sync' ? 'artifact_sync' : 'runtime_output') as 'artifact_sync' | 'runtime_output',
                removable: Boolean(item.removable),
            }))
            .filter((item) => item.path);
        const seen = new Set(systemPinned.map((item) => item.path));
        const custom = customFolders
            .map((folder) => normalizeFolderPath(folder))
            .filter((folder) => folder && !seen.has(folder))
            .map((folder) => ({
                path: folder,
                label: folder.split('/').filter(Boolean).pop() || folder,
                kind: 'custom' as const,
                removable: true,
            }));
        // v5.6: user-pinned entries (via context menu) bubble to the top of the `custom`
        // bucket. System-pinned (runtime_output / artifact_sync) always stay at the very top.
        const userPinnedCustom: FolderEntry[] = [];
        const unpinnedCustom: FolderEntry[] = [];
        for (const item of custom) {
            (pinnedPaths.has(item.path) ? userPinnedCustom : unpinnedCustom).push(item);
        }
        return [...systemPinned, ...userPinnedCustom, ...unpinnedCustom];
    }, [customFolders, rootsInfo?.folders, pinnedPaths]);

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

    const addFolderByPath = useCallback(async (rawPath: string) => {
        const normalized = normalizeFolderPath(rawPath);
        if (!normalized) {
            setInlineError(lang === 'zh' ? '路径不合法' : 'Invalid path');
            return false;
        }
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
            setRecentlyAddedPath(resolvedRoot);
            return true;
        } catch (e) {
            const msg = String(e).slice(0, 200);
            setError(msg);
            setInlineError(msg);
            return false;
        } finally {
            setLoading(false);
        }
    }, [fetchTreeData, lang]);

    const handleAddFolder = useCallback(async () => {
        // v5.8: open inline input FIRST — guarantees user sees UI feedback
        // even if the native picker fails/cancels/gets hidden behind other
        // windows. If native picker returns a path, auto-fill it.
        setInlineAction('addRoot');
        setInlineName(activeFolder || outputDir || '');
        setInlineError('');
        showToast(
            t('Paste the absolute path and press Enter', '粘贴绝对路径后按 Enter 确认'),
            'info',
        );
        const desktopApi = getDesktopApi();
        if (desktopApi?.pickFolder) {
            try {
                const picked = String(await desktopApi.pickFolder(activeFolder || outputDir || '')).trim();
                if (picked) {
                    // Auto-submit via addFolderByPath so user can just confirm.
                    const ok = await addFolderByPath(picked);
                    if (ok) {
                        showToast(t('Folder added', '已添加文件夹'), 'success');
                        setInlineAction(null);
                        setInlineName('');
                    } else {
                        // addFolderByPath already set inlineError; keep inline open.
                        setInlineName(picked);
                        showToast(
                            t('Backend rejected that folder — check the path or pick another', '后端拒绝了这个目录 · 请检查路径或换一个'),
                            'error',
                        );
                    }
                    return;
                }
                // User cancelled the native dialog — inline input is already up, let them type.
            } catch (err) {
                setInlineError(String(err).slice(0, 160));
            }
        }
    }, [activeFolder, addFolderByPath, getDesktopApi, outputDir, showToast, t]);

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
    // v5.7: No more window.prompt()/alert() (Electron renderer blocks them).
    // These handlers surface an inline input row at the top of the file tree
    // and use the toast component for status feedback.
    const handleNewFolder = useCallback(() => {
        if (!activeFolder) {
            showToast(t('Select a folder first', '请先选择一个文件夹'), 'error');
            return;
        }
        setInlineAction('folder');
        setInlineName('');
        setInlineError('');
    }, [activeFolder, showToast, t]);

    const handleNewFile = useCallback(() => {
        if (!activeFolder) {
            showToast(t('Select a folder first', '请先选择一个文件夹'), 'error');
            return;
        }
        setInlineAction('file');
        setInlineName('');
        setInlineError('');
    }, [activeFolder, showToast, t]);

    const submitInlineCreate = useCallback(async () => {
        const value = inlineName.trim();
        if (!value) {
            setInlineError(t('Name cannot be empty', '名称不能为空'));
            return;
        }
        if (inlineAction === 'addRoot') {
            const ok = await addFolderByPath(value);
            if (ok) {
                showToast(t(`Folder added: ${value}`, `已添加并激活: ${value}`), 'success');
                setInlineAction(null);
                setInlineName('');
                setInlineError('');
            } else {
                // addFolderByPath sets inlineError; also show a persistent toast so
                // the user notices even if the inline row is off-screen.
                showToast(
                    t(
                        `Could not add that folder — the backend rejected the path.`,
                        `无法添加这个目录 · 后端拒绝了这个路径`,
                    ),
                    'error',
                );
            }
            return;
        }
        if (/[/\\]/.test(value)) {
            setInlineError(t('Name cannot contain / or \\', '名称不能包含 / 或 \\'));
            return;
        }
        const endpoint = inlineAction === 'folder' ? 'mkdir' : 'write';
        const body = inlineAction === 'folder'
            ? { root: activeFolder, path: value }
            : { root: activeFolder, path: value, content: '' };
        try {
            const res = await fetch(`${API_BASE}/api/workspace/${endpoint}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                const errText = (await res.text()).slice(0, 200);
                setInlineError(errText || `HTTP ${res.status}`);
                return;
            }
            showToast(
                inlineAction === 'folder'
                    ? t(`Folder created: ${value}`, `文件夹已创建: ${value}`)
                    : t(`File created: ${value}`, `文件已创建: ${value}`),
                'success',
            );
            setInlineAction(null);
            setInlineName('');
            setInlineError('');
            window.dispatchEvent(new CustomEvent('evermind:workspace-updated'));
        } catch (e) {
            setInlineError(String(e).slice(0, 200));
        }
    }, [inlineAction, inlineName, activeFolder, showToast, t, addFolderByPath]);

    const cancelInlineCreate = useCallback(() => {
        setInlineAction(null);
        setInlineName('');
        setInlineError('');
    }, []);

    const handleCollapseAll = useCallback(() => {
        setExpandedDirs(new Set());
        showToast(t('All folders collapsed', '已折叠所有文件夹'), 'info');
    }, [showToast, t]);

    // v5.6: Drag-and-drop uploader — supports files AND whole folders via webkitGetAsEntry.
    const handleDrop = useCallback(async (ev: React.DragEvent) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (!activeFolder) {
            showToast(lang === 'zh' ? '请先选择一个文件夹' : 'Select a target folder first', 'error');
            return;
        }
        const dt = ev.dataTransfer;
        if (!dt) return;
        const collected: Array<{ relPath: string; file: File }> = [];
        const itemList = Array.from(dt.items || []);
        if (itemList.length > 0 && typeof (itemList[0] as DataTransferItem & { webkitGetAsEntry?: () => FileSystemEntry | null }).webkitGetAsEntry === 'function') {
            for (const it of itemList) {
                if (it.kind !== 'file') continue;
                const entry = (it as DataTransferItem & { webkitGetAsEntry?: () => FileSystemEntry | null }).webkitGetAsEntry?.();
                if (!entry) continue;
                try {
                    const files = await walkFileSystemEntry(entry);
                    collected.push(...files);
                } catch (err) {
                    console.warn('Failed to walk dropped entry:', err);
                }
            }
        } else {
            // Fallback: plain file list (no folder descent).
            const plain = Array.from(dt.files || []);
            plain.forEach((f) => collected.push({ relPath: f.name, file: f }));
        }
        if (collected.length === 0) return;

        // Upload sequentially: progress tracks completed bytes as each file finishes.
        const totalBytes = collected.reduce((sum, c) => sum + (c.file.size || 0), 0);
        let uploadedBytes = 0;
        setUploadProgress({ loaded: 0, total: totalBytes, currentFile: collected[0].relPath, done: 0, totalFiles: collected.length });
        for (let i = 0; i < collected.length; i++) {
            const { relPath, file } = collected[i];
            setUploadProgress((prev) => prev ? { ...prev, currentFile: relPath, done: i } : prev);
            const { ok, error } = await uploadFileBase64(activeFolder, relPath, file);
            uploadedBytes += file.size || 0;
            setUploadProgress((prev) => prev ? { ...prev, loaded: uploadedBytes } : prev);
            if (!ok) {
                console.warn(`Upload failed for ${relPath}: ${error}`);
            }
        }
        setUploadProgress(null);
        window.dispatchEvent(new CustomEvent('evermind:workspace-updated'));
    }, [activeFolder, lang, showToast]);

    const handleDragOver = useCallback((ev: React.DragEvent) => {
        ev.preventDefault();
        if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
    }, []);

    return (
        <div
            style={{
                display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden',
                background: UI.sidebarBg, position: 'relative',
                fontFamily: UI.sansStack, color: UI.rowText,
            }}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
        >
            {/* v5.6 Upload progress overlay */}
            {uploadProgress && (
                <div style={{
                    position: 'absolute', left: 8, right: 8, bottom: 8, zIndex: 10,
                    background: 'linear-gradient(180deg, rgba(17,24,39,0.96), rgba(2,6,23,0.96))',
                    border: '1px solid rgba(56,189,248,0.3)', borderRadius: 8,
                    padding: '10px 12px', boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
                    fontSize: 11, color: '#e6edf3',
                }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                        <span style={{ fontWeight: 600, color: '#7dd3fc' }}>
                            {t('Uploading', '上传中')} {uploadProgress.done + 1}/{uploadProgress.totalFiles}
                        </span>
                        <span style={{ color: '#94a3b8', fontFamily: 'ui-monospace, monospace' }}>
                            {Math.round((uploadProgress.loaded / Math.max(1, uploadProgress.total)) * 100)}%
                        </span>
                    </div>
                    <div style={{ fontSize: 10, color: '#94a3b8', marginBottom: 6, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {uploadProgress.currentFile}
                    </div>
                    <div style={{
                        height: 4, background: 'rgba(255,255,255,0.08)', borderRadius: 3, overflow: 'hidden',
                    }}>
                        <div style={{
                            width: `${(uploadProgress.loaded / Math.max(1, uploadProgress.total)) * 100}%`,
                            height: '100%',
                            background: 'linear-gradient(90deg, #38bdf8, #0ea5e9)',
                            transition: 'width 120ms ease',
                        }} />
                    </div>
                </div>
            )}
            {/* v5.6 Context menu portal */}
            {ctxMenu && <ContextMenu x={ctxMenu.x} y={ctxMenu.y} items={ctxMenu.items} onClose={() => setCtxMenu(null)} />}
            {/* v5.7 Toast — Electron blocks alert(), so we draw our own. */}
            {toast && (
                <div
                    role="status"
                    style={{
                        position: 'absolute', right: 12, bottom: 14, zIndex: 20,
                        minWidth: 180, maxWidth: 340,
                        padding: '8px 12px',
                        background: toast.kind === 'error'
                            ? 'linear-gradient(180deg, rgba(127,29,29,0.96), rgba(69,10,10,0.96))'
                            : toast.kind === 'success'
                                ? 'linear-gradient(180deg, rgba(20,83,45,0.96), rgba(5,46,22,0.96))'
                                : 'linear-gradient(180deg, rgba(17,24,39,0.96), rgba(2,6,23,0.96))',
                        border: `1px solid ${toast.kind === 'error' ? 'rgba(248,113,113,0.45)' : toast.kind === 'success' ? 'rgba(74,222,128,0.45)' : 'rgba(148,163,184,0.3)'}`,
                        borderRadius: 8,
                        boxShadow: '0 10px 32px rgba(0,0,0,0.55)',
                        color: '#e6edf3', fontSize: 11, lineHeight: 1.4,
                        display: 'flex', alignItems: 'flex-start', gap: 8,
                    }}
                    onClick={() => setToast(null)}
                >
                    <span style={{
                        width: 6, height: 6, borderRadius: '50%', marginTop: 5, flexShrink: 0,
                        background: toast.kind === 'error' ? '#ef4444' : toast.kind === 'success' ? '#22c55e' : '#38bdf8',
                        boxShadow: `0 0 6px ${toast.kind === 'error' ? '#ef4444' : toast.kind === 'success' ? '#22c55e' : '#38bdf8'}88`,
                    }} />
                    <span style={{ flex: 1, minWidth: 0, wordBreak: 'break-word' }}>{toast.message}</span>
                </div>
            )}
            {/* Explorer header — VS Code Dark Modern: flat bg, uppercase 11px label, codicon-style action buttons */}
            <div style={{
                display: 'flex', alignItems: 'center',
                height: UI.rowHeight + 8, padding: '0 12px 0 20px',
                borderBottom: `1px solid ${UI.sectionBorder}`, flexShrink: 0,
                background: UI.sidebarBg,
            }}>
                <span style={{
                    fontSize: 11, fontWeight: 700, color: UI.rowTextMuted,
                    textTransform: 'uppercase', letterSpacing: '0.04em', flex: 1,
                }}>{t('Explorer', 'Explorer')}</span>
                <div style={{ display: 'flex', gap: 2 }}>
                    {[
                        {
                            svg: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M10.5 1.5H3.5A1.5 1.5 0 0 0 2 3v10A1.5 1.5 0 0 0 3.5 14.5h9A1.5 1.5 0 0 0 14 13V5L10.5 1.5Z"/><path d="M10 1.5V5h4M8 8v4M6 10h4"/></svg>),
                            title: t('New File', '新建文件'), action: handleNewFile,
                        },
                        {
                            svg: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M2 5V3.5A1.5 1.5 0 0 1 3.5 2h3.379a1.5 1.5 0 0 1 1.06.44L9.5 4h3A1.5 1.5 0 0 1 14 5.5V12a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12V5Z"/><path d="M8 8v4M6 10h4"/></svg>),
                            title: t('New Folder', '新建文件夹'), action: handleNewFolder,
                        },
                        {
                            svg: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 8a5.5 5.5 0 0 1 9.9-3.3M13.5 8a5.5 5.5 0 0 1-9.9 3.3"/><path d="M12.5 1.5v3h-3M3.5 14.5v-3h3"/></svg>),
                            title: t('Refresh', '刷新'), action: () => window.dispatchEvent(new CustomEvent('evermind:workspace-updated')),
                        },
                        {
                            svg: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M3 4h10M3 8h10M3 12h10"/></svg>),
                            title: t('Collapse All', '全部折叠'), action: handleCollapseAll,
                        },
                    ].map((btn, i) => (
                        <button key={i} onClick={btn.action} title={btn.title} style={{
                            width: 22, height: 22, display: 'flex', alignItems: 'center', justifyContent: 'center',
                            background: 'transparent', border: 'none', cursor: 'pointer', borderRadius: 3,
                            color: UI.rowTextMuted,
                        }}
                        onMouseEnter={e => { e.currentTarget.style.background = UI.rowHoverBg; e.currentTarget.style.color = UI.rowText; }}
                        onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = UI.rowTextMuted; }}
                        >{btn.svg}</button>
                    ))}
                </div>
            </div>

            {/* Workspace root picker — VS Code-style "MY-PROJECT" uppercase section header.
                One dropdown row per workspace; click to switch, right-click for actions.
                Replaces the prior two duplicate folder lists. */}
            {folderEntries.length > 0 && (
                <>
                <div style={{
                    flexShrink: 0, padding: '6px 12px',
                    fontSize: 10, lineHeight: 1.4,
                    color: UI.rowTextDim,
                    background: UI.sidebarBg,
                    borderBottom: `1px solid ${UI.sectionBorder}`,
                }}>
                    {t(
                        'Click a folder below to activate it. Only the ACTIVE folder receives AI-generated code.',
                        '点下方文件夹激活它 · 只有「激活中」那一个会接收 AI 写入的代码',
                    )}
                </div>
                <div style={{ flexShrink: 0, borderBottom: `1px solid ${UI.sectionBorder}` }}>
                    {folderEntries.map((folder) => {
                        const isActive = activeFolder === folder.path;
                        const isPinned = pinnedPaths.has(folder.path);
                        const isFlash = recentlyAddedPath && recentlyAddedPath === folder.path;
                        // v5.8: translate backend label + kind into a human name + purpose line,
                        // so users don't get lost between the opaque "CURRENT" / "DELIVERY" titles.
                        let friendlyLabel: string;
                        let purposeLine: string;
                        if (folder.kind === 'runtime_output') {
                            friendlyLabel = t('Runtime Output (system temp)', '运行时输出（系统临时）');
                            purposeLine = t('Auto cleared each run — do not put your own files here', '每轮自动清空 · 不要往里放自己的文件');
                        } else if (folder.kind === 'artifact_sync') {
                            friendlyLabel = t('Delivery Folder · AI writes here', '交付目录 · AI 代码写入这里');
                            purposeLine = t('Your real project dir — code syncs here in real-time', '你的项目目录 · 代码实时同步到这里');
                        } else {
                            friendlyLabel = folder.label || folder.path.split('/').pop() || folder.path;
                            purposeLine = t('Click to activate — AI will write here next run', '点击激活 · 下轮 AI 代码会写入这里');
                        }
                        return (
                            <div
                                key={folder.path}
                                onClick={() => setActiveFolder(folder.path)}
                                onContextMenu={(e) => {
                                    e.preventDefault();
                                    const items: ContextMenuItem[] = [
                                        {
                                            label: isPinned ? t('Unpin', '取消置顶') : t('Pin to top', '置顶'),
                                            icon: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M8 1v6M5 7h6l-1 4H6L5 7ZM8 11v4"/></svg>),
                                            onClick: () => togglePin(folder.path),
                                        },
                                        {
                                            label: t('Copy path', '复制路径'),
                                            icon: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="4" width="9" height="11" rx="1.5"/><path d="M3 12V3a1.5 1.5 0 0 1 1.5-1.5H11"/></svg>),
                                            onClick: () => { void navigator.clipboard.writeText(folder.path); },
                                        },
                                        {
                                            label: t('Reveal in Finder', '在访达中显示'),
                                            icon: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M2 13V5a1.5 1.5 0 0 1 1.5-1.5h3l1 1.5h5A1.5 1.5 0 0 1 14 6.5V13a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 13Z"/></svg>),
                                            onClick: () => { const api = getDesktopApi(); if (api?.revealInFinder) void api.revealInFinder(folder.path); },
                                            disabled: !getDesktopApi()?.revealInFinder,
                                        },
                                        { kind: 'separator' },
                                        {
                                            label: t('Refresh', '刷新'),
                                            icon: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 8a5.5 5.5 0 0 1 9.9-3.3M13.5 8a5.5 5.5 0 0 1-9.9 3.3"/><path d="M12.5 1.5v3h-3M3.5 14.5v-3h3"/></svg>),
                                            onClick: () => window.dispatchEvent(new CustomEvent('evermind:workspace-updated')),
                                        },
                                    ];
                                    if (folder.removable) {
                                        items.push({ kind: 'separator' });
                                        items.push({
                                            label: t('Remove from list', '从列表移除'),
                                            icon: (<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>),
                                            danger: true,
                                            onClick: () => handleRemoveFolder(folder.path),
                                        });
                                    }
                                    setCtxMenu({ x: e.clientX, y: e.clientY, items });
                                }}
                                title={folder.path}
                                style={{
                                    display: 'flex', alignItems: 'flex-start', gap: 8,
                                    padding: '8px 10px 8px 14px',
                                    cursor: 'pointer', userSelect: 'none',
                                    color: isActive ? UI.rowText : UI.rowTextMuted,
                                    background: isFlash
                                        ? 'rgba(76,183,130,0.22)'    // v5.8: green flash for just-added folder
                                        : isActive ? UI.rowHoverBg : 'transparent',
                                    borderLeft: isFlash
                                        ? '2px solid #4CB782'
                                        : isActive ? `2px solid ${UI.focusRing}` : '2px solid transparent',
                                    transition: 'background 400ms ease, border-color 400ms ease',
                                }}
                                onMouseEnter={(e) => { if (!isActive) (e.currentTarget as HTMLDivElement).style.background = UI.rowHoverBg; }}
                                onMouseLeave={(e) => { if (!isActive) (e.currentTarget as HTMLDivElement).style.background = 'transparent'; }}
                            >
                                <div style={{ paddingTop: 2 }}>
                                    <Chevron expanded={isActive} color={isActive ? UI.rowText : UI.rowTextMuted} />
                                </div>
                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                        <span style={{
                                            flex: 1, minWidth: 0, fontSize: 12, fontWeight: 600,
                                            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                                            color: isActive ? UI.rowText : UI.rowTextMuted,
                                        }}>
                                            {friendlyLabel}
                                        </span>
                                        {isActive && (
                                            <span style={{
                                                fontSize: 9, fontWeight: 700, letterSpacing: '0.05em',
                                                padding: '1px 6px', borderRadius: 3,
                                                background: `${UI.focusRing}30`,
                                                color: '#B8D1FF',
                                                whiteSpace: 'nowrap',
                                                flexShrink: 0,
                                            }}>
                                                {t('ACTIVE', '激活中')}
                                            </span>
                                        )}
                                        {isPinned && (
                                            <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor" style={{ opacity: 0.7, color: UI.rowTextMuted, flexShrink: 0 }} aria-hidden>
                                                <path d="M8 1.5a1 1 0 0 0-1 1V6L4.2 8.8a.7.7 0 0 0-.2.5V10h3.3v4.5a.5.5 0 0 0 1 0V10h3.3v-.7a.7.7 0 0 0-.2-.5L9 6V2.5a1 1 0 0 0-1-1Z"/>
                                            </svg>
                                        )}
                                        {folder.removable && (
                                            <button
                                                onClick={(e) => { e.stopPropagation(); handleRemoveFolder(folder.path); }}
                                                title={t('Remove', '移除')}
                                                style={{
                                                    background: 'none', border: 'none', cursor: 'pointer',
                                                    color: UI.rowTextMuted, fontSize: 14, padding: '0 2px',
                                                    lineHeight: 1, flexShrink: 0,
                                                }}
                                            >
                                                ×
                                            </button>
                                        )}
                                    </div>
                                    <div style={{
                                        fontSize: 10, lineHeight: 1.4, marginTop: 2,
                                        color: UI.rowTextDim,
                                        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                                    }}>
                                        {purposeLine}
                                    </div>
                                </div>
                            </div>
                        );
                    })}
                    {/* v5.8: on success the CTA fully morphs into a compact
                        "folder icon + name + ✓ added" chip — the card shape
                        goes away so users see a clear "this is now a folder,
                        not a button" state. After 2.5s it reverts to the CTA
                        so users can add more folders. */}
                    {recentlyAddedPath ? (
                        <>
                            {/* v5.8.1: persistent "added" chip — stays visible until
                                the user replaces it. Click to add a different folder
                                (fires handleAddFolder, success will overwrite this chip). */}
                            <div
                                title={recentlyAddedPath}
                                onClick={handleAddFolder}
                                style={{
                                    margin: '8px 10px 4px',
                                    padding: '8px 10px',
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: 8,
                                    background: 'rgba(76,183,130,0.10)',
                                    borderLeft: '2px solid #4CB782',
                                    borderRadius: 3,
                                    cursor: 'pointer',
                                    transition: 'background 120ms',
                                }}
                                onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = 'rgba(76,183,130,0.18)'; }}
                                onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = 'rgba(76,183,130,0.10)'; }}
                            >
                                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
                                    <path d="M1.5 13V4.5A1.5 1.5 0 0 1 3 3h3.17a1.5 1.5 0 0 1 1.06.44L8.5 4.5H13A1.5 1.5 0 0 1 14.5 6v7A1.5 1.5 0 0 1 13 14.5H3A1.5 1.5 0 0 1 1.5 13Z"
                                        stroke="#4CB782" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" fill="rgba(76,183,130,0.18)"/>
                                </svg>
                                <span style={{
                                    flex: 1, minWidth: 0, fontSize: 12, fontWeight: 500,
                                    color: UI.rowText,
                                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                                }}>
                                    {recentlyAddedPath.split('/').pop() || recentlyAddedPath}
                                </span>
                                <span style={{
                                    fontSize: 9, fontWeight: 700, letterSpacing: '0.04em',
                                    color: '#4CB782', whiteSpace: 'nowrap', flexShrink: 0,
                                }}>
                                    ✓ {t('ADDED', '已添加')}
                                </span>
                            </div>
                            <div style={{
                                margin: '0 10px 10px', padding: '2px 10px',
                                fontSize: 10, color: UI.rowTextDim, display: 'flex', alignItems: 'center', gap: 4,
                            }}>
                                <span>{t('Click card to change folder · or', '点卡片更换文件夹 · 或')}</span>
                                <a
                                    onClick={(e) => { e.stopPropagation(); setRecentlyAddedPath(''); try { window.localStorage.removeItem('evermind-last-added-folder'); } catch {} }}
                                    style={{ color: UI.focusRing, cursor: 'pointer', textDecoration: 'underline' }}
                                >
                                    {t('clear', '清除')}
                                </a>
                            </div>
                        </>
                    ) : (
                        <div
                            onClick={handleAddFolder}
                            title={t('Add a folder where AI will write the generated code', '添加一个让 AI 写入代码的文件夹')}
                            style={{
                                margin: '8px 10px 10px',
                                padding: '10px 12px',
                                borderRadius: 6,
                                border: `1px dashed ${UI.focusRing}66`,
                                background: `${UI.focusRing}10`,
                                cursor: 'pointer',
                                display: 'flex',
                                alignItems: 'center',
                                gap: 10,
                                transition: 'background 120ms, border-color 120ms',
                            }}
                            onMouseEnter={(e) => {
                                const el = e.currentTarget as HTMLDivElement;
                                el.style.background = `${UI.focusRing}22`;
                                el.style.borderColor = UI.focusRing;
                            }}
                            onMouseLeave={(e) => {
                                const el = e.currentTarget as HTMLDivElement;
                                el.style.background = `${UI.focusRing}10`;
                                el.style.borderColor = `${UI.focusRing}66`;
                            }}
                        >
                            <div style={{
                                width: 28, height: 28, flexShrink: 0,
                                borderRadius: 6,
                                background: `${UI.focusRing}25`,
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                                color: UI.focusRing,
                            }}>
                                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                                    <path d="M8 3v10M3 8h10"/>
                                </svg>
                            </div>
                            <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{
                                    fontSize: 12, fontWeight: 600,
                                    color: UI.rowText,
                                    marginBottom: 2,
                                }}>
                                    {t('Add Code Output Folder', '添加代码写入文件夹')}
                                </div>
                                <div style={{
                                    fontSize: 10, lineHeight: 1.4,
                                    color: UI.rowTextMuted,
                                    whiteSpace: 'nowrap',
                                    overflow: 'hidden',
                                    textOverflow: 'ellipsis',
                                }}>
                                    {t(
                                        'AI will save generated code here in real-time',
                                        'AI 生成的代码会实时同步到这个目录',
                                    )}
                                </div>
                            </div>
                        </div>
                    )}
                </div>
                </>
            )}

            {/* Search — flat inset field, VS Code filter-box style */}
            {activeFolder && (
                <div style={{ padding: '6px 12px', flexShrink: 0, background: UI.sidebarBg }}>
                    <input
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder={t('Search files...', '搜索文件...')}
                        style={{
                            width: '100%', padding: '3px 8px', fontSize: 12, lineHeight: '20px',
                            background: '#0E1013', border: '1px solid transparent',
                            borderRadius: 2, color: UI.rowText, outline: 'none',
                            fontFamily: UI.sansStack,
                        }}
                        onFocus={(e) => { e.currentTarget.style.border = `1px solid ${UI.focusRing}`; }}
                        onBlur={(e) => { e.currentTarget.style.border = '1px solid transparent'; }}
                    />
                </div>
            )}

            {notice && (
                <div style={{ padding: '4px 12px', fontSize: 11, color: '#81B88B', flexShrink: 0 }}>
                    {notice}
                </div>
            )}
            {error && (
                <div style={{ padding: '4px 12px', fontSize: 11, color: '#C74E39', flexShrink: 0 }}>
                    {error}
                </div>
            )}

            {/* Inline input row — shown when the user hits "New File" / "New Folder" / "+" (Electron blocks window.prompt) */}
            {inlineAction && (
                <div style={{
                    padding: '6px 12px', flexShrink: 0, background: UI.sidebarBg,
                    borderBottom: `1px solid ${UI.sectionBorder}`,
                }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{
                            fontSize: 10, fontWeight: 700, color: UI.rowTextMuted, letterSpacing: '0.04em',
                            textTransform: 'uppercase', flexShrink: 0,
                        }}>
                            {inlineAction === 'folder' ? t('NEW FOLDER', '新建文件夹')
                                : inlineAction === 'file' ? t('NEW FILE', '新建文件')
                                : t('ADD ROOT', '添加根目录')}
                        </span>
                        <input
                            ref={inlineInputRef}
                            value={inlineName}
                            onChange={(e) => setInlineName(e.target.value)}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter') { e.preventDefault(); void submitInlineCreate(); }
                                else if (e.key === 'Escape') { e.preventDefault(); cancelInlineCreate(); }
                            }}
                            placeholder={
                                inlineAction === 'folder' ? t('folder-name', '文件夹名')
                                : inlineAction === 'file' ? t('file.ext', 'file.ext')
                                : t('/absolute/path/to/folder', '/绝对路径/到/文件夹')
                            }
                            style={{
                                flex: 1, padding: '3px 8px', fontSize: 12, lineHeight: '20px',
                                background: '#0E1013', border: `1px solid ${UI.focusRing}`,
                                borderRadius: 2, color: UI.rowText, outline: 'none',
                                fontFamily: inlineAction === 'addRoot' ? 'ui-monospace, monospace' : UI.sansStack,
                            }}
                        />
                        <button
                            onClick={() => void submitInlineCreate()}
                            title={t('Confirm (Enter)', '确认 (Enter)')}
                            style={{
                                padding: '3px 10px', fontSize: 11, fontWeight: 600,
                                background: UI.focusRing, color: '#fff',
                                border: 'none', borderRadius: 2, cursor: 'pointer',
                            }}
                        >
                            {t('OK', '确认')}
                        </button>
                        <button
                            onClick={cancelInlineCreate}
                            title={t('Cancel (Esc)', '取消 (Esc)')}
                            style={{
                                padding: '3px 8px', fontSize: 11,
                                background: '#0E1013', color: UI.rowText,
                                border: 'none', borderRadius: 2, cursor: 'pointer',
                            }}
                        >
                            {t('Cancel', '取消')}
                        </button>
                    </div>
                    {inlineError && (
                        <div style={{ marginTop: 4, fontSize: 11, color: '#C74E39' }}>
                            {inlineError}
                        </div>
                    )}
                </div>
            )}

            <div style={{ flex: 1, overflow: 'auto', background: UI.sidebarBg }}>
                {!activeFolder && folderEntries.length === 0 && (
                    <div style={{ padding: '32px 20px', textAlign: 'center' }}>
                        <div style={{ fontSize: 12, color: UI.rowTextMuted, marginBottom: 14, lineHeight: 1.6 }}>
                            {t('No folder opened yet', '还没有打开文件夹')}
                        </div>
                        <div style={{ fontSize: 11, color: UI.rowTextDim, lineHeight: 1.6, marginBottom: 16 }}>
                            {t(
                                'The current run output will appear here automatically. You can also open your own folder.',
                                '当前运行输出会自动出现在这里，也可以手动打开自己的文件夹。'
                            )}
                        </div>
                        <button
                            onClick={handleAddFolder}
                            style={{
                                padding: '4px 12px', fontSize: 12,
                                background: UI.focusRing, color: '#fff',
                                border: 'none', borderRadius: 2, cursor: 'pointer', fontFamily: UI.sansStack,
                            }}
                        >
                            {t('Open Folder', '打开文件夹')}
                        </button>
                    </div>
                )}

                {loading && tree.length === 0 && (
                    <div style={{ padding: '12px 16px', fontSize: 12, color: UI.rowTextMuted }}>
                        {t('Loading...', '加载中...')}
                    </div>
                )}

                {activeFolder && !loading && filteredTree.length === 0 && !error && (
                    <div style={{ padding: '12px 16px', fontSize: 12, color: UI.rowTextMuted }}>
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
                    padding: '3px 12px',
                    flexShrink: 0,
                    borderTop: `1px solid ${UI.sectionBorder}`,
                    fontSize: 11,
                    color: UI.rowTextDim,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    background: UI.sidebarBg,
                    height: 22,
                }}>
                    <span>{totalFiles} {t('files', '文件')}</span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                        {getDesktopApi()?.revealInFinder && (
                            <button
                                onClick={() => void handleRevealFolder()}
                                style={{
                                    background: 'none', border: 'none', cursor: 'pointer',
                                    color: UI.rowTextDim, fontSize: 11, padding: 0,
                                    fontFamily: UI.sansStack,
                                }}
                                onMouseEnter={(e) => (e.currentTarget.style.color = UI.rowText)}
                                onMouseLeave={(e) => (e.currentTarget.style.color = UI.rowTextDim)}
                                title={t('Reveal in Finder', '在访达中显示')}
                            >
                                {t('Finder', '访达')}
                            </button>
                        )}
                        <button
                            onClick={() => void refresh()}
                            style={{
                                background: 'none', border: 'none', cursor: 'pointer',
                                color: UI.rowTextDim, padding: 0, display: 'flex',
                            }}
                            onMouseEnter={(e) => (e.currentTarget.style.color = UI.rowText)}
                            onMouseLeave={(e) => (e.currentTarget.style.color = UI.rowTextDim)}
                            title={t('Refresh', '刷新')}
                        >
                            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M2.5 8a5.5 5.5 0 0 1 9.9-3.3M13.5 8a5.5 5.5 0 0 1-9.9 3.3"/>
                                <path d="M12.5 1.5v3h-3M3.5 14.5v-3h3"/>
                            </svg>
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
