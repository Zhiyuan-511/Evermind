/* Evermind — TypeScript Type Definitions */

export interface Port {
    id: string;
    label: string;
}

export interface NodeData {
    id: string;
    type: string;
    name: string;
    x: number;
    y: number;
    inputs: Port[];
    outputs: Port[];
    status: 'idle' | 'running' | 'done' | 'error';
    progress: number;
    prompt?: string;
    model?: string;
    lastOutput?: string;
    plugins?: string[];
    log: LogEntry[];
}

export interface Edge {
    from: string; // output port id
    to: string;   // input port id
}

export interface LogEntry {
    ts: number;
    msg: string;
    type: 'info' | 'error' | 'warn' | 'ok' | 'sys';
}

export interface Workflow {
    id: string;
    name: string;
    description?: string;
    nodes: NodeData[];
    edges: Edge[];
    created_at?: string;
    updated_at?: string;
}

export interface SubTask {
    id: string;
    agent: string;
    task: string;
    status: string;
    depends_on: string[];
    retries?: number;
    output_preview?: string;
    error?: string;
}

export interface OrchestratorReport {
    success: boolean;
    goal: string;
    total_subtasks: number;
    completed: number;
    failed: number;
    total_retries: number;
    duration_seconds: number;
    subtasks: SubTask[];
}

export interface ChatMessage {
    id: string;
    role: 'user' | 'system' | 'agent';
    content: string;
    sender?: string;
    icon?: string;
    timestamp: string;
    borderColor?: string;
}

export interface ModelInfo {
    id: string;
    provider: string;
    litellm_id: string;
    supports_tools: boolean;
    supports_cua: boolean;
}

// Node type definitions with icons and colors
export const NODE_TYPES: Record<string, { icon: string; color: string; label_en: string; label_zh: string }> = {
    router: { icon: '🔀', color: '#4f8fff', label_en: 'Router', label_zh: '路由器' },
    planner: { icon: '📋', color: '#a855f7', label_en: 'Planner', label_zh: '规划师' },
    builder: { icon: '👷', color: '#40d67c', label_en: 'Builder', label_zh: '构建者' },
    tester: { icon: '🧪', color: '#ff9a40', label_en: 'Tester', label_zh: '测试员' },
    reviewer: { icon: '👁', color: '#06b6d4', label_en: 'Reviewer', label_zh: '审查员' },
    deployer: { icon: '🚀', color: '#ec4899', label_en: 'Deployer', label_zh: '部署员' },
    debugger: { icon: '🔧', color: '#f59e0b', label_en: 'Debugger', label_zh: '调试器' },
    analyst: { icon: '📊', color: '#8b5cf6', label_en: 'Analyst', label_zh: '分析师' },
    scribe: { icon: '📝', color: '#14b8a6', label_en: 'Scribe', label_zh: '记录员' },
    monitor: { icon: '📡', color: '#6366f1', label_en: 'Monitor', label_zh: '监控器' },
    localshell: { icon: '💻', color: '#64748b', label_en: 'Shell', label_zh: '终端' },
    fileread: { icon: '📄', color: '#78716c', label_en: 'File Read', label_zh: '读文件' },
    filewrite: { icon: '💾', color: '#78716c', label_en: 'File Write', label_zh: '写文件' },
    screenshot: { icon: '📸', color: '#f472b6', label_en: 'Screenshot', label_zh: '截图' },
    browser: { icon: '🌐', color: '#fb923c', label_en: 'Browser', label_zh: '浏览器' },
    gitops: { icon: '🔗', color: '#a3e635', label_en: 'Git', label_zh: 'Git操作' },
    uicontrol: { icon: '🖱', color: '#c084fc', label_en: 'UI Control', label_zh: 'UI控制' },
    imagegen: { icon: '🎨', color: '#f43f5e', label_en: 'Image Gen', label_zh: '图像生成' },
    bgremove: { icon: '✂️', color: '#10b981', label_en: 'BG Remove', label_zh: '去背景' },
    spritesheet: { icon: '🖼', color: '#7c3aed', label_en: 'Spritesheet', label_zh: '精灵表' },
    assetimport: { icon: '📦', color: '#0ea5e9', label_en: 'Asset Import', label_zh: '素材导入' },
    merger: { icon: '🔗', color: '#6b7280', label_en: 'Merger', label_zh: '合并器' },
};
