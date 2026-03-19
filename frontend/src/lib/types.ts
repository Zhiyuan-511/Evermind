/* Evermind — TypeScript Type Definitions */

export interface Port {
    id: string;
    label: string;
}

export type CanvasNodeStatus =
    | 'idle'
    | 'queued'
    | 'running'
    | 'passed'
    | 'failed'
    | 'blocked'
    | 'waiting_approval'
    | 'skipped'
    | 'done'
    | 'error';

export interface NodeData {
    id: string;
    type: string;
    name: string;
    x: number;
    y: number;
    inputs: Port[];
    outputs: Port[];
    status: CanvasNodeStatus;
    progress: number;
    prompt?: string;
    model?: string;
    assignedModel?: string;
    lastOutput?: string;
    outputSummary?: string;
    plugins?: string[];
    nodeType?: string;
    label?: string;
    lang?: 'en' | 'zh';
    subtaskId?: string;
    taskDescription?: string;
    nodeExecutionId?: string;
    tokensUsed?: number;
    cost?: number;
    startedAt?: number;
    endedAt?: number;
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

export interface ChatHistorySession {
    id: string;
    title: string;
    createdAt: number;
    updatedAt: number;
    messages: ChatMessage[];
}

export interface RunSubtaskReport {
    id: string;
    agent: string;
    status: string;
    retries: number;
    task: string;
    outputPreview: string;
    error: string;
    durationSeconds?: number;
    startedAt?: number;
    endedAt?: number;
    timelineEvents?: string[];
    workSummary?: string[];
    filesCreated?: string[];
}

export interface RunReportRecord {
    id: string;
    taskId?: string;
    createdAt: number;
    goal: string;
    difficulty: 'simple' | 'standard' | 'pro';
    success: boolean;
    totalSubtasks: number;
    completed: number;
    failed: number;
    totalRetries: number;
    durationSeconds: number;
    previewUrl?: string;
    subtasks: RunSubtaskReport[];
}

export interface ModelInfo {
    id: string;
    provider: string;
    litellm_id: string;
    supports_tools: boolean;
    supports_cua: boolean;
}

// Node type definitions with icons, colors, ports, descriptions, and security levels
export interface NodeTypeInfo {
    icon: string;
    color: string;
    label_en: string;
    label_zh: string;
    desc_en: string;
    desc_zh: string;
    inputs: { id: string; label: string }[];
    outputs: { id: string; label: string }[];
    sec?: string; // L1, L2, L3, L4
}

export const NODE_TYPES: Record<string, NodeTypeInfo> = {
    router: { icon: '🔀', color: '#4f8fff', label_en: 'Router', label_zh: '路由器', desc_en: 'Intake & Dispatch', desc_zh: '接收分发', inputs: [{ id: 'task', label: 'Task' }], outputs: [{ id: 'dispatch', label: 'Dispatch' }] },
    planner: { icon: '📋', color: '#a855f7', label_en: 'Planner', label_zh: '规划师', desc_en: 'Architecture', desc_zh: '架构设计', inputs: [{ id: 'goal', label: 'Goal' }], outputs: [{ id: 'plan', label: 'Plan' }] },
    builder: { icon: '👷', color: '#40d67c', label_en: 'Builder', label_zh: '构建者', desc_en: 'Engineer', desc_zh: '代码工程', inputs: [{ id: 'spec', label: 'Spec' }], outputs: [{ id: 'code', label: 'Code' }] },
    tester: { icon: '🧪', color: '#ff9a40', label_en: 'Tester', label_zh: '测试员', desc_en: 'QA', desc_zh: '质量测试', inputs: [{ id: 'code', label: 'Code' }], outputs: [{ id: 'report', label: 'Report' }] },
    reviewer: { icon: '👁', color: '#06b6d4', label_en: 'Reviewer', label_zh: '审查员', desc_en: 'Gatekeeper', desc_zh: '质量审核', inputs: [{ id: 'input', label: 'Input' }], outputs: [{ id: 'verdict', label: 'Verdict' }] },
    deployer: { icon: '🚀', color: '#ec4899', label_en: 'Deployer', label_zh: '部署员', desc_en: 'DevOps', desc_zh: '运维部署', inputs: [{ id: 'artifact', label: 'Artifact' }], outputs: [{ id: 'url', label: 'URL' }] },
    debugger: { icon: '🔧', color: '#f59e0b', label_en: 'Debugger', label_zh: '调试器', desc_en: 'Bug Hunter', desc_zh: '错误追踪', inputs: [{ id: 'error', label: 'Error' }], outputs: [{ id: 'fix', label: 'Fix' }] },
    analyst: { icon: '📊', color: '#8b5cf6', label_en: 'Analyst', label_zh: '分析师', desc_en: 'Data', desc_zh: '数据分析', inputs: [{ id: 'data', label: 'Data' }], outputs: [{ id: 'report', label: 'Report' }] },
    scribe: { icon: '📝', color: '#14b8a6', label_en: 'Scribe', label_zh: '记录员', desc_en: 'Writer', desc_zh: '文档撰写', inputs: [{ id: 'content', label: 'Content' }], outputs: [{ id: 'doc', label: 'Doc' }] },
    monitor: { icon: '📡', color: '#6366f1', label_en: 'Monitor', label_zh: '监控器', desc_en: 'Watch', desc_zh: '系统监控', inputs: [{ id: 'signal', label: 'Signal' }], outputs: [{ id: 'alert', label: 'Alert' }] },
    localshell: { icon: '💻', color: '#64748b', label_en: 'Shell', label_zh: '终端', desc_en: 'Shell', desc_zh: '终端命令', inputs: [{ id: 'cmd', label: 'Command' }], outputs: [{ id: 'stdout', label: 'Output' }], sec: 'L3' },
    fileread: { icon: '📄', color: '#78716c', label_en: 'File Read', label_zh: '读文件', desc_en: 'Read', desc_zh: '读取文件', inputs: [{ id: 'path', label: 'Path' }], outputs: [{ id: 'content', label: 'Content' }], sec: 'L1' },
    filewrite: { icon: '💾', color: '#78716c', label_en: 'File Write', label_zh: '写文件', desc_en: 'Write', desc_zh: '写入文件', inputs: [{ id: 'content', label: 'Content' }], outputs: [{ id: 'path', label: 'Path' }], sec: 'L2' },
    screenshot: { icon: '📸', color: '#f472b6', label_en: 'Screenshot', label_zh: '截图', desc_en: 'Capture', desc_zh: '屏幕截图', inputs: [{ id: 'trigger', label: 'Trigger' }], outputs: [{ id: 'image', label: 'Image' }], sec: 'L1' },
    browser: { icon: '🌐', color: '#fb923c', label_en: 'Browser', label_zh: '浏览器', desc_en: 'Web', desc_zh: '网页浏览', inputs: [{ id: 'url', label: 'URL' }], outputs: [{ id: 'page', label: 'Page' }], sec: 'L2' },
    gitops: { icon: '🔗', color: '#a3e635', label_en: 'Git', label_zh: 'Git操作', desc_en: 'Git', desc_zh: 'Git管理', inputs: [{ id: 'action', label: 'Action' }], outputs: [{ id: 'result', label: 'Result' }], sec: 'L2' },
    uicontrol: { icon: '🖱', color: '#c084fc', label_en: 'UI Control', label_zh: 'UI控制', desc_en: 'Mouse/KB', desc_zh: '鼠标/键盘', inputs: [{ id: 'action', label: 'Action' }], outputs: [{ id: 'result', label: 'Result' }], sec: 'L3' },
    imagegen: { icon: '🎨', color: '#f43f5e', label_en: 'Image Gen', label_zh: '图像生成', desc_en: 'Generate', desc_zh: '生成图像', inputs: [{ id: 'prompt', label: 'Prompt' }], outputs: [{ id: 'image', label: 'Image' }] },
    bgremove: { icon: '✂️', color: '#10b981', label_en: 'BG Remove', label_zh: '去背景', desc_en: 'RemoveBG', desc_zh: '移除背景', inputs: [{ id: 'image', label: 'Image' }], outputs: [{ id: 'result', label: 'Result' }] },
    spritesheet: { icon: '🖼', color: '#7c3aed', label_en: 'Spritesheet', label_zh: '精灵表', desc_en: 'Pack', desc_zh: '打包精灵', inputs: [{ id: 'images', label: 'Images' }], outputs: [{ id: 'sheet', label: 'Sheet' }] },
    assetimport: { icon: '📦', color: '#0ea5e9', label_en: 'Asset Import', label_zh: '素材导入', desc_en: 'Import', desc_zh: '导入素材', inputs: [{ id: 'asset', label: 'Asset' }], outputs: [{ id: 'result', label: 'Result' }] },
    merger: { icon: '🔗', color: '#6b7280', label_en: 'Merger', label_zh: '合并器', desc_en: 'Merge', desc_zh: '合并数据', inputs: [{ id: 'a', label: 'Input A' }, { id: 'b', label: 'Input B' }], outputs: [{ id: 'merged', label: 'Merged' }] },
};

// ─── Kanban Task Board ──────────────────────────
export type TaskStatus = 'backlog' | 'planned' | 'executing' | 'review' | 'selfcheck' | 'done';

export type TaskPriority = 'low' | 'medium' | 'high' | 'urgent';
export type TaskMode = 'standard' | 'pro' | 'debug' | 'review';

export interface SelfCheckItem {
    name: string;
    passed: boolean;
    detail: string;
}

export interface TaskCard {
    id: string;
    title: string;
    description: string;
    status: TaskStatus;
    mode: TaskMode;
    owner: string;
    progress: number;
    priority: TaskPriority;
    createdAt: number;
    updatedAt: number;
    version?: number;
    runIds: string[];
    relatedFiles: string[];
    latestSummary: string;
    latestRisk: string;
    reviewVerdict: string;
    reviewIssues: string[];
    selfcheckItems: SelfCheckItem[];
    reports?: RunReportRecord[];
}

export const TASK_COLUMNS: { key: TaskStatus; label_en: string; label_zh: string; color: string; icon: string }[] = [
    { key: 'backlog',   label_en: 'Backlog',    label_zh: '待办',   color: '#64748b', icon: '📥' },
    { key: 'planned',   label_en: 'Planned',    label_zh: '已规划', color: '#a855f7', icon: '📋' },
    { key: 'executing', label_en: 'Executing',  label_zh: '执行中', color: '#3b82f6', icon: '⚙️' },
    { key: 'review',    label_en: 'Review',     label_zh: '审核',   color: '#f59e0b', icon: '👁' },
    { key: 'selfcheck', label_en: 'Self-Check', label_zh: '自检',   color: '#06b6d4', icon: '🧪' },
    { key: 'done',      label_en: 'Done',       label_zh: '完成',   color: '#22c55e', icon: '✅' },
];

// ─────────────────────────────────────────
// V1 Canonical Types: Run / NodeExecution / Artifact
// ─────────────────────────────────────────
export type RunStatus = 'queued' | 'running' | 'waiting_review' | 'waiting_selfcheck' | 'failed' | 'done' | 'cancelled';
export type NodeExecutionStatus = 'queued' | 'running' | 'passed' | 'failed' | 'blocked' | 'waiting_approval' | 'skipped' | 'cancelled';
export type TriggerSource = 'openclaw' | 'ui' | 'api' | 'retry' | 'resume';
export type RunRuntime = 'local' | 'openclaw';
export type ReviewDecision = 'approve' | 'reject' | 'needs_fix' | 'blocked';
export type ValidationStatus = 'passed' | 'failed' | 'skipped' | 'blocked';
export type ArtifactType = 'changed_files' | 'diff_summary' | 'report' | 'review_result' | 'test_output' | 'build_output' | 'run_summary' | 'risk_report' | 'deployment_notes' | 'raw_log' | 'preview_ref';

export interface RunRecord {
    id: string;
    task_id: string;
    status: RunStatus;
    trigger_source: TriggerSource;
    runtime: RunRuntime;
    workflow_template_id: string;
    current_node_execution_id: string;
    active_node_execution_ids?: string[];
    started_at: number;
    ended_at: number;
    total_tokens: number;
    total_cost: number;
    summary: string;
    risks: string[];
    node_execution_ids: string[];
    created_at: number;
    updated_at: number;
    version?: number;
    timeout_seconds?: number;
}

export interface NodeExecutionRecord {
    id: string;
    run_id: string;
    node_key: string;
    node_label: string;
    retried_from_id: string;
    status: NodeExecutionStatus;
    assigned_model: string;
    assigned_provider: string;
    input_summary: string;
    output_summary: string;
    error_message: string;
    retry_count: number;
    tokens_used: number;
    cost: number;
    started_at: number;
    ended_at: number;
    artifact_ids: string[];
    created_at: number;
    updated_at: number;
    progress?: number;
    phase?: string;
    version?: number;
    timeout_seconds?: number;
    depends_on_keys?: string[];
}

export interface ArtifactRecord {
    id: string;
    run_id: string;
    node_execution_id: string;
    artifact_type: ArtifactType;
    title: string;
    path: string;
    content: string;
    metadata: Record<string, unknown>;
    created_at: number;
}

export interface ReviewDecisionRecord {
    id: string;
    run_id: string;
    node_execution_id: string;
    decision: ReviewDecision;
    issues: string[];
    remaining_risks: string[];
    next_action: string;
    created_at: number;
}

export interface ValidationResultRecord {
    id: string;
    run_id: string;
    node_execution_id: string;
    summary_status: ValidationStatus;
    checklist: { name: string; status: string; detail?: string }[];
    summary: string;
    created_at: number;
}
