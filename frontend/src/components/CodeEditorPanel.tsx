'use client';

import React, { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import MonacoEditor, { DiffEditor, type BeforeMount } from '@monaco-editor/react';
import type { editor } from 'monaco-editor';
import type * as Monaco from 'monaco-editor';

// Initialize Monaco environment for Electron compatibility (must be before any editor render)
import '@/lib/monaco-env';

// ── Custom Evermind Dark Theme ──
const EVERMIND_THEME_NAME = 'evermind-dark';

function defineEvermindTheme(monaco: typeof Monaco) {
    monaco.editor.defineTheme(EVERMIND_THEME_NAME, {
        base: 'vs-dark',
        inherit: true,
        rules: [
            // Comments — muted sage
            { token: 'comment', foreground: '6a9955', fontStyle: 'italic' },
            { token: 'comment.block', foreground: '6a9955', fontStyle: 'italic' },
            // Keywords — soft purple/violet
            { token: 'keyword', foreground: 'c586c0' },
            { token: 'keyword.control', foreground: 'c586c0' },
            { token: 'keyword.operator', foreground: 'c586c0' },
            // Storage / types
            { token: 'storage', foreground: '569cd6' },
            { token: 'storage.type', foreground: '569cd6' },
            // Strings — warm orange
            { token: 'string', foreground: 'ce9178' },
            { token: 'string.escape', foreground: 'd7ba7d' },
            // Numbers
            { token: 'number', foreground: 'b5cea8' },
            { token: 'number.hex', foreground: 'b5cea8' },
            // Functions — light yellow
            { token: 'entity.name.function', foreground: 'dcdcaa' },
            { token: 'support.function', foreground: 'dcdcaa' },
            // Variables
            { token: 'variable', foreground: '9cdcfe' },
            { token: 'variable.predefined', foreground: '4fc1ff' },
            // Types / classes — teal green
            { token: 'type', foreground: '4ec9b0' },
            { token: 'entity.name.type', foreground: '4ec9b0' },
            { token: 'entity.name.class', foreground: '4ec9b0' },
            // Tags (HTML/XML)
            { token: 'tag', foreground: '569cd6' },
            { token: 'tag.id', foreground: '569cd6' },
            { token: 'metatag', foreground: '569cd6' },
            { token: 'metatag.content', foreground: 'ce9178' },
            // Attributes
            { token: 'attribute.name', foreground: '9cdcfe' },
            { token: 'attribute.value', foreground: 'ce9178' },
            // Constants
            { token: 'constant', foreground: '4fc1ff' },
            { token: 'constant.language', foreground: '569cd6' },
            // Operators
            { token: 'operator', foreground: 'd4d4d4' },
            // Delimiters
            { token: 'delimiter', foreground: '808080' },
            { token: 'delimiter.bracket', foreground: 'ffd700' },
            // Regex
            { token: 'regexp', foreground: 'd16969' },
            // Decorators
            { token: 'annotation', foreground: 'dcdcaa' },
            // Markdown
            { token: 'markup.heading', foreground: '569cd6', fontStyle: 'bold' },
            { token: 'markup.bold', fontStyle: 'bold' },
            { token: 'markup.italic', fontStyle: 'italic' },
        ],
        colors: {
            // Editor background — matches Evermind's #0d1117
            'editor.background': '#0d1117',
            'editor.foreground': '#e6edf3',
            // Selection
            'editor.selectionBackground': '#264f78',
            'editor.inactiveSelectionBackground': '#1d3b5c',
            'editor.selectionHighlightBackground': '#264f7844',
            // Current line
            'editor.lineHighlightBackground': '#161b2280',
            'editor.lineHighlightBorder': '#1e2430',
            // Line numbers
            'editorLineNumber.foreground': '#484f58',
            'editorLineNumber.activeForeground': '#8b949e',
            // Gutter
            'editorGutter.background': '#0d1117',
            // Cursor
            'editorCursor.foreground': '#58a6ff',
            // Bracket matching
            'editorBracketMatch.background': '#3b82f633',
            'editorBracketMatch.border': '#3b82f699',
            // Indent guides
            'editorIndentGuide.background': '#21262d',
            'editorIndentGuide.activeBackground': '#484f58',
            // Whitespace
            'editorWhitespace.foreground': '#21262d',
            // Scrollbar
            'scrollbar.shadow': '#00000000',
            'scrollbarSlider.background': '#484f5833',
            'scrollbarSlider.hoverBackground': '#484f5866',
            'scrollbarSlider.activeBackground': '#484f5899',
            // Minimap
            'minimap.background': '#0d1117',
            'minimapSlider.background': '#484f5833',
            // Overview ruler
            'editorOverviewRuler.border': '#0d1117',
            // Widget (autocomplete, hover)
            'editorWidget.background': '#161b22',
            'editorWidget.border': '#30363d',
            // Find
            'editor.findMatchBackground': '#f2cc6044',
            'editor.findMatchHighlightBackground': '#f2cc6022',
        },
    });
}

const handleBeforeMount: BeforeMount = (monaco) => {
    defineEvermindTheme(monaco);
};

// ── Types ──
export interface OpenFile {
    path: string;
    name: string;
    content: string;
    ext: string;
    rootFolder?: string;
    originalContent?: string;
    modified?: boolean;
}

interface CodeEditorPanelProps {
    openFiles: OpenFile[];
    activeFileIndex: number;
    onSwitchFile: (index: number) => void;
    onCloseFile: (index: number) => void;
    onSaveFile?: (file: OpenFile) => Promise<void>;
    onUpdateFileContent?: (index: number, content: string) => void;
    lang: 'en' | 'zh';
}

// ── Extension → Monaco language ID ──
const EXT_TO_MONACO_LANG: Record<string, string> = {
    js: 'javascript', jsx: 'javascript', mjs: 'javascript',
    ts: 'typescript', tsx: 'typescript',
    py: 'python', html: 'html', htm: 'html', xml: 'xml', svg: 'xml',
    css: 'css', scss: 'scss', less: 'less',
    json: 'json', md: 'markdown',
    sh: 'shell', bash: 'shell', zsh: 'shell',
    yaml: 'yaml', yml: 'yaml', sql: 'sql',
    go: 'go', rs: 'rust', java: 'java',
    c: 'c', cpp: 'cpp', h: 'c', hpp: 'cpp',
    lua: 'lua', rb: 'ruby', php: 'php', swift: 'swift',
    kt: 'kotlin', dart: 'dart', r: 'r',
    dockerfile: 'dockerfile', graphql: 'graphql',
};

const EXT_COLORS: Record<string, string> = {
    js: '#f1e05a', ts: '#3178c6', tsx: '#3178c6', jsx: '#f1e05a',
    py: '#3572a5', html: '#e34c26', css: '#563d7c', json: '#292929',
    md: '#083fa1', sh: '#89e051', go: '#00add8', rs: '#dea584',
    java: '#b07219', rb: '#701516', php: '#4f5d95', swift: '#f05138',
    lua: '#000080', cpp: '#f34b7d', c: '#555555',
};

function normalizeExt(ext: string): string {
    return ext.toLowerCase().replace(/^\./, '');
}

function getMonacoLang(ext: string): string {
    return EXT_TO_MONACO_LANG[normalizeExt(ext)] || 'plaintext';
}

// ── Breadcrumb ──
function Breadcrumb({ path }: { path: string }) {
    const parts = path.replace(/^\//, '').split('/');
    return (
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 10, color: '#8b949e', padding: '4px 12px', borderBottom: '1px solid rgba(255,255,255,0.06)', whiteSpace: 'nowrap', overflow: 'hidden' }}>
            {parts.map((p, i) => (
                <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                    {i > 0 && <span style={{ color: '#484f58' }}>/</span>}
                    <span style={i === parts.length - 1 ? { color: '#e6edf3', fontWeight: 500 } : {}}>{p}</span>
                </span>
            ))}
        </div>
    );
}

// ── Main Component ──
export default function CodeEditorPanel({
    openFiles, activeFileIndex, onSwitchFile, onCloseFile, onSaveFile, onUpdateFileContent, lang,
}: CodeEditorPanelProps) {
    const [viewMode, setViewMode] = useState<'code' | 'diff'>('code');
    const [saving, setSaving] = useState(false);
    const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
    const activeFile = openFiles[activeFileIndex] || null;
    const hasDiff = activeFile?.originalContent != null;
    const effectiveMode = hasDiff && viewMode === 'diff' ? 'diff' : 'code';
    const t = useCallback((zh: string, en: string) => lang === 'zh' ? zh : en, [lang]);

    // Cmd+S save
    useEffect(() => {
        const h = (e: KeyboardEvent) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 's') {
                e.preventDefault();
                if (activeFile?.modified && onSaveFile) {
                    setSaving(true);
                    onSaveFile(activeFile).finally(() => setSaving(false));
                }
            }
        };
        window.addEventListener('keydown', h);
        return () => window.removeEventListener('keydown', h);
    }, [activeFile, onSaveFile]);

    const handleEditorMount = useCallback((ed: editor.IStandaloneCodeEditor) => {
        editorRef.current = ed;
    }, []);

    const handleEditorChange = useCallback((value: string | undefined) => {
        if (value !== undefined) {
            onUpdateFileContent?.(activeFileIndex, value);
        }
    }, [activeFileIndex, onUpdateFileContent]);

    // Monaco editor options
    const editorOptions: editor.IStandaloneEditorConstructionOptions = useMemo(() => ({
        fontSize: 13,
        fontFamily: "'JetBrains Mono','Fira Code','SF Mono','Cascadia Code',Menlo,monospace",
        fontLigatures: true,
        lineHeight: 20,
        minimap: { enabled: true, scale: 1, showSlider: 'mouseover' },
        scrollBeyondLastLine: false,
        renderWhitespace: 'selection',
        bracketPairColorization: { enabled: true },
        guides: { bracketPairs: true, indentation: true },
        smoothScrolling: true,
        cursorBlinking: 'smooth',
        cursorSmoothCaretAnimation: 'on',
        padding: { top: 8, bottom: 8 },
        automaticLayout: true,
        tabSize: 2,
        wordWrap: 'off',
        folding: true,
        lineNumbers: 'on',
        renderLineHighlight: 'line',
        contextmenu: true,
        overviewRulerBorder: false,
        scrollbar: {
            verticalScrollbarSize: 10,
            horizontalScrollbarSize: 10,
            useShadows: false,
        },
    }), []);

    const diffOptions: editor.IDiffEditorConstructionOptions = useMemo(() => ({
        ...editorOptions,
        renderSideBySide: true,
        originalEditable: false,
        readOnly: true,
    }), [editorOptions]);

    if (!openFiles.length) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#8b949e', background: '#0d1117' }}>
                <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 32, opacity: 0.2, marginBottom: 12 }}>
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    </div>
                    <div style={{ fontSize: 12 }}>{t('在左侧文件树中点击文件打开', 'Click a file to open')}</div>
                </div>
            </div>
        );
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%', background: '#0d1117' }}>
            {/* Tab bar */}
            <div style={{ display: 'flex', alignItems: 'center', borderBottom: '1px solid #21262d', background: '#161b22', flexShrink: 0, minHeight: 35 }}>
                <div style={{ display: 'flex', flex: 1, overflow: 'auto' }}>
                    {openFiles.map((f, i) => (
                        <button key={f.path} onClick={() => onSwitchFile(i)} style={{
                            display: 'flex', alignItems: 'center', gap: 6, padding: '6px 12px', fontSize: 11,
                            border: 'none', cursor: 'pointer', whiteSpace: 'nowrap',
                            background: i === activeFileIndex ? '#0d1117' : '#161b22',
                            color: i === activeFileIndex ? '#e6edf3' : '#8b949e',
                            borderRight: '1px solid #21262d',
                            borderTop: i === activeFileIndex ? '2px solid #58a6ff' : '2px solid transparent',
                        }}>
                            <span style={{ width: 6, height: 6, borderRadius: '50%', background: EXT_COLORS[normalizeExt(f.ext)] || '#8b949e' }} />
                            {f.name}
                            {f.modified && <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#e6edf3', opacity: 0.7 }} />}
                            <span onClick={e => { e.stopPropagation(); onCloseFile(i); }}
                                style={{ marginLeft: 4, opacity: 0.3, cursor: 'pointer', fontSize: 13 }}
                                onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
                                onMouseLeave={e => (e.currentTarget.style.opacity = '0.3')}>×</span>
                        </button>
                    ))}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '0 8px', flexShrink: 0 }}>
                    {hasDiff && ['code', 'diff'].map(m => (
                        <button key={m} onClick={() => setViewMode(m as 'code' | 'diff')} style={{
                            fontSize: 10, padding: '2px 8px', borderRadius: 3, border: 'none', cursor: 'pointer',
                            background: effectiveMode === m ? 'rgba(88,166,255,0.2)' : 'rgba(255,255,255,0.04)',
                            color: effectiveMode === m ? '#58a6ff' : '#8b949e',
                        }}>{m === 'code' ? 'Code' : 'Diff'}</button>
                    ))}
                    {activeFile?.modified && (
                        <button onClick={() => { if (activeFile && onSaveFile) { setSaving(true); onSaveFile(activeFile).finally(() => setSaving(false)); } }}
                            style={{ fontSize: 10, padding: '3px 10px', borderRadius: 3, border: 'none', cursor: 'pointer',
                                background: '#238636', color: '#fff', fontWeight: 600 }}>
                            {saving ? '...' : 'Save'}
                        </button>
                    )}
                </div>
            </div>

            {activeFile && <Breadcrumb path={activeFile.path} />}

            {/* Editor content */}
            <div style={{ flex: 1, minHeight: 0 }}>
                {activeFile && effectiveMode === 'code' && (
                    <MonacoEditor
                        key={activeFile.path}
                        language={getMonacoLang(activeFile.ext)}
                        value={activeFile.content}
                        theme={EVERMIND_THEME_NAME}
                        beforeMount={handleBeforeMount}
                        options={editorOptions}
                        onChange={handleEditorChange}
                        onMount={handleEditorMount}
                        loading={
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#8b949e', background: '#0d1117' }}>
                                <span style={{ fontSize: 12 }}>{t('编辑器加载中...', 'Loading editor...')}</span>
                            </div>
                        }
                    />
                )}
                {activeFile && effectiveMode === 'diff' && (
                    <DiffEditor
                        original={activeFile.originalContent || ''}
                        modified={activeFile.content}
                        language={getMonacoLang(activeFile.ext)}
                        theme={EVERMIND_THEME_NAME}
                        beforeMount={handleBeforeMount}
                        options={diffOptions}
                        loading={
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#8b949e', background: '#0d1117' }}>
                                <span style={{ fontSize: 12 }}>{t('差异视图加载中...', 'Loading diff view...')}</span>
                            </div>
                        }
                    />
                )}
            </div>
        </div>
    );
}
