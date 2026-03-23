'use client';

interface GuideModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
}

const SHORTCUTS = [
    ['Ctrl+Enter', { en: 'Run Workflow', zh: '运行工作流' }],
    ['Ctrl+Z / Y', { en: 'Undo / Redo', zh: '撤销 / 重做' }],
    ['Ctrl+S', { en: 'Save', zh: '保存' }],
    ['Delete', { en: 'Delete node', zh: '删除节点' }],
    ['Space+Drag', { en: 'Pan Canvas', zh: '平移画布' }],
    ['Scroll', { en: 'Zoom', zh: '缩放' }],
] as const;

const FEATURE_GUIDE = [
    {
        title: { en: 'Difficulty Mode', zh: '难度模式' },
        detail: {
            en: 'Simple uses 2-3 nodes for speed, Standard uses 3-4 nodes for balance, Pro uses 5-7 nodes for higher quality and stricter review.',
            zh: 'Simple 使用 2-3 个节点追求速度；Standard 使用 3-4 个节点做平衡；Pro 使用 5-7 个节点追求更高质量与更严格审查。',
        },
    },
    {
        title: { en: 'Playwright Smoke Test', zh: 'Playwright 烟雾测试' },
        detail: {
            en: 'Runs a real headless browser check in tester stage to catch white screen / broken render issues. Turn off for faster iteration.',
            zh: '在 Tester 阶段用真实无头浏览器做页面验收，可发现白屏/渲染异常。关闭后速度更快，但可视化把关会变弱。',
        },
    },
    {
        title: { en: 'Max Retries', zh: '最大重试次数' },
        detail: {
            en: 'Defines how many retries a failing subtask can take. Higher value improves success rate but increases time and cost.',
            zh: '控制失败子任务最多重试几次。值越大成功率通常更高，但耗时和成本也会增加。',
        },
    },
    {
        title: { en: 'Auto Model Downgrade', zh: '自动模型降级' },
        detail: {
            en: 'When a subtask fails, the system can switch to backup models and retry automatically to avoid full workflow interruption.',
            zh: '子任务失败时会自动切换备用模型并重试，降低整条工作流中断的概率。',
        },
    },
    {
        title: { en: 'Canvas Preview', zh: '画布预览' },
        detail: {
            en: 'After HTML output is generated, preview auto-opens inside canvas. You can refresh, open in new tab, or jump back to nodes.',
            zh: '生成 HTML 后会自动切到画布内预览。可直接刷新、在新窗口打开，或一键返回节点视图。',
        },
    },
    {
        title: { en: 'History Sessions', zh: '历史会话' },
        detail: {
            en: 'All chat progress is stored by session. Supports search and rename to continue unfinished projects later.',
            zh: '聊天与任务会按会话保存，支持搜索和重命名，方便中断后继续推进项目。',
        },
    },
    {
        title: { en: 'Diagnostics', zh: '诊断面板' },
        detail: {
            en: 'Use diagnostics to check backend connectivity, latest preview validation, and quick troubleshooting signals.',
            zh: '诊断面板可检查后端连通性、最新预览验收结果和关键故障信号，便于快速排障。',
        },
    },
] as const;

const RECOMMENDED_PRESETS = [
    {
        useCase: { en: 'Fast iteration while building', zh: '开发阶段快速迭代' },
        settings: { en: 'Simple / Smoke OFF / Max Retries 2-3', zh: 'Simple / 关闭 Smoke / 重试 2-3 次' },
    },
    {
        useCase: { en: 'Pre-release validation', zh: '交付前验收' },
        settings: { en: 'Pro / Smoke ON / Max Retries 3-5', zh: 'Pro / 开启 Smoke / 重试 3-5 次' },
    },
    {
        useCase: { en: 'Low cost, stable output', zh: '低成本稳定输出' },
        settings: { en: 'Standard / Smoke ON / Max Retries 2-3', zh: 'Standard / 开启 Smoke / 重试 2-3 次' },
    },
] as const;

export default function GuideModal({ open, onClose, lang }: GuideModalProps) {
    if (!open) return null;

    const t = (en: string, zh: string) => lang === 'zh' ? zh : en;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-container" onClick={e => e.stopPropagation()}>
                <div className="modal-header">
                    <h3>{t('User Guide', '使用说明')}</h3>
                    <button className="modal-close" onClick={onClose}>✕</button>
                </div>
                <div className="modal-body guide-body">
                    {/* Getting Started */}
                    <div className="guide-section">
                        <h4>{t('Getting Started', '开始使用')}</h4>
                        <ol>
                            <li>{t('Drag AI agent nodes from the left panel onto the canvas', '从左侧节点面板拖拽AI智能体到画布上')}</li>
                            <li>{t('Connect nodes by dragging from output port to input port', '用鼠标从输出端口拖向输入端口来连接节点')}</li>
                            <li>{t('Click ▶ Run button to execute the workflow', '点击 ▶ 运行 按钮执行工作流')}</li>
                            <li>{t('Type a task in the chat panel to send goals directly', '在聊天面板输入任务描述发布任务')}</li>
                        </ol>
                    </div>

                    {/* Core Features Explained */}
                    <div className="guide-section">
                        <h4>{t('Core Features Explained', '核心功能解释')}</h4>
                        <div className="guide-feature-grid">
                            {FEATURE_GUIDE.map((item) => (
                                <div className="guide-feature-card" key={item.title.en}>
                                    <div className="guide-feature-title">
                                        <strong>{lang === 'zh' ? item.title.zh : item.title.en}</strong>
                                    </div>
                                    <p>{lang === 'zh' ? item.detail.zh : item.detail.en}</p>
                                </div>
                            ))}
                        </div>
                    </div>

                    {/* Recommended Presets */}
                    <div className="guide-section">
                        <h4>{t('Recommended Presets', '推荐配置')}</h4>
                        <table className="guide-table">
                            <thead>
                                <tr>
                                    <th>{t('Use Case', '使用场景')}</th>
                                    <th>{t('Recommended Settings', '推荐设置')}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {RECOMMENDED_PRESETS.map((preset) => (
                                    <tr key={preset.useCase.en}>
                                        <td>{lang === 'zh' ? preset.useCase.zh : preset.useCase.en}</td>
                                        <td>{lang === 'zh' ? preset.settings.zh : preset.settings.en}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>

                    {/* How to Publish Tasks */}
                    <div className="guide-section">
                        <h4>{t('How to Publish Tasks', '如何发布任务')}</h4>
                        <ol>
                            <li>{t('Click the "Tasks" tab in the right panel', '在右侧面板点击"任务"标签')}</li>
                            <li>{t('Type your task (e.g. "Build a simple webpage")', '输入任务描述（如："帮我写一个简单的网页"）')}</li>
                            <li>{t('Press Enter or click Send', '按回车或点击发送')}</li>
                            <li>{t('AI will automatically plan, distribute, and execute', 'AI会自动规划、分发和执行任务')}</li>
                        </ol>
                    </div>

                    {/* Node Types */}
                    <div className="guide-section">
                        <h4>{t('Node Types', '节点类型')}</h4>
                        <div className="guide-cols">
                            <div>
                                <strong>{t('AI Agents', 'AI 智能体')} (9)</strong>
                                <ul>
                                    <li>{t('Router — Task dispatch', '路由器 — 任务接收分发')}</li>
                                    <li>{t('Planner — Architecture', '规划师 — 架构设计')}</li>
                                    <li>{t('Reviewer — Code review', '审查员 — 质量审核')}</li>
                                    <li>{t('Builder — Write code', '构建者 — 编写代码')}</li>
                                    <li>{t('Tester — QA', '测试员 — 质量测试')}</li>
                                    <li>{t('Deployer — CI/CD', '部署者 — 运维部署')}</li>
                                    <li>{t('Analyst — Data', '分析师 — 数据分析')}</li>
                                    <li>{t('Scribe — Docs', '记录员 — 文档撰写')}</li>
                                    <li>{t('Debugger — Bug fix', '调试器 — 错误追踪')}</li>
                                </ul>
                            </div>
                            <div>
                                <strong>{t('Local Execution', '本地执行')} (7)</strong>
                                <ul>
                                    <li>{t('Shell (L3)', '终端 (L3)')}</li>
                                    <li>{t('FileRead (L1)', '读文件 (L1)')}</li>
                                    <li>{t('FileWrite (L2)', '写文件 (L2)')}</li>
                                    <li>{t('Screenshot (L1)', '截图 (L1)')}</li>
                                    <li>{t('Browser (L2)', '浏览器 (L2)')}</li>
                                    <li>{t('Git (L2)', 'Git操作 (L2)')}</li>
                                    <li>{t('UIControl (L3)', 'UI控制 (L3)')}</li>
                                </ul>
                            </div>
                        </div>
                    </div>

                    {/* Security */}
                    <div className="guide-section">
                        <h4>{t('Security Levels', '安全等级')}</h4>
                        <div className="guide-sec-grid">
                            <div className="guide-sec-item l1"><strong>L1</strong> {t('Read-only — No confirmation', '只读 — 无需确认')}</div>
                            <div className="guide-sec-item l2"><strong>L2</strong> {t('File/Network — Auto-approve configurable', '文件/网络 — 可配置自动批准')}</div>
                            <div className="guide-sec-item l3"><strong>L3</strong> {t('Confirm Required — Dialog before execution', '需确认 — 执行前弹出确认')}</div>
                            <div className="guide-sec-item l4"><strong>L4</strong> {t('Password + Countdown', '需密码 + 倒计时')}</div>
                        </div>
                    </div>

                    {/* Shortcuts */}
                    <div className="guide-section">
                        <h4>{t('Shortcuts', '快捷键')}</h4>
                        <table className="guide-table">
                            <thead><tr><th>{t('Key', '按键')}</th><th>{t('Action', '操作')}</th></tr></thead>
                            <tbody>
                                {SHORTCUTS.map(([key, labels]) => (
                                    <tr key={key}>
                                        <td><kbd>{key}</kbd></td>
                                        <td>{lang === 'zh' ? labels.zh : labels.en}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>

                    {/* Tips */}
                    <div className="guide-section">
                        <h4>{t('Tips', '使用技巧')}</h4>
                        <ul>
                            <li>{t('Use Templates dropdown to load presets quickly', '使用模板库快速加载常用工作流')}</li>
                            <li>{t('Configure API keys in Settings → Connection', '在设置 → 连接中配置API密钥')}</li>
                            <li>{t('Read Settings → Quality carefully before long tasks', '执行长任务前先看设置 → 验收策略中的参数说明')}</li>
                            <li>{t('Right-click nodes for context menu', '右键节点可复制/删除/测试')}</li>
                            <li>{t('Import/Export configs in Settings', '设置中可导入/导出配置文件')}</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>
    );
}
