'use client';

export interface TemplateDef {
    key: string;
    icon: string;
    title_en: string;
    title_zh: string;
    desc_en: string;
    desc_zh: string;
    tags: string[];
    nodes: { type: string; x: number; y: number }[];
    edges: [number, number][];
}

export const TEMPLATES: TemplateDef[] = [
    {
        key: 'webdev', icon: '🌐',
        title_en: 'Web Development', title_zh: 'Web开发',
        desc_en: 'Full-stack web app with auto-testing and deployment.',
        desc_zh: '全栈Web应用，含自动测试和部署。',
        tags: ['Frontend', 'Backend', 'CI/CD'],
        nodes: [
            { type: 'router', x: 50, y: 200 }, { type: 'planner', x: 280, y: 200 },
            { type: 'reviewer', x: 510, y: 200 }, { type: 'builder', x: 740, y: 120 },
            { type: 'builder', x: 740, y: 300 }, { type: 'tester', x: 970, y: 200 },
            { type: 'deployer', x: 1200, y: 200 },
        ],
        edges: [[0, 1], [1, 2], [2, 3], [2, 4], [3, 5], [4, 5], [5, 6]],
    },
    {
        key: 'artpipe', icon: '🎨',
        title_en: 'Art Asset Pipeline', title_zh: '美术管线',
        desc_en: 'Complete game art pipeline from generation to import.',
        desc_zh: '完整的游戏美术管线，从生成到导入。',
        tags: ['ImageGen', 'BGRemove', 'Sprites'],
        nodes: [
            { type: 'router', x: 50, y: 160 }, { type: 'imagegen', x: 280, y: 30 },
            { type: 'imagegen', x: 280, y: 160 }, { type: 'imagegen', x: 280, y: 290 },
            { type: 'bgremove', x: 510, y: 160 }, { type: 'spritesheet', x: 740, y: 160 },
            { type: 'assetimport', x: 970, y: 160 },
        ],
        edges: [[0, 1], [0, 2], [0, 3], [1, 4], [2, 4], [3, 4], [4, 5], [5, 6]],
    },
    {
        key: 'bugfix', icon: '🔧',
        title_en: 'Automated Bug Fix', title_zh: '自动修Bug',
        desc_en: 'Auto-detect, analyze, fix, test, and commit bugs.',
        desc_zh: '自动检测、分析、修复、测试和提交Bug。',
        tags: ['Debug', 'Git', 'Shell'],
        nodes: [
            { type: 'screenshot', x: 50, y: 200 }, { type: 'debugger', x: 280, y: 200 },
            { type: 'planner', x: 510, y: 200 }, { type: 'reviewer', x: 740, y: 200 },
            { type: 'builder', x: 970, y: 200 }, { type: 'localshell', x: 1200, y: 120 },
            { type: 'gitops', x: 1200, y: 300 }, { type: 'deployer', x: 1430, y: 200 },
        ],
        edges: [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [4, 6], [5, 7], [6, 7]],
    },
    {
        key: 'vidprod', icon: '🎬',
        title_en: 'Video Production', title_zh: '视频制作',
        desc_en: 'Automated video editing workflow.',
        desc_zh: '自动化视频编辑工作流。',
        tags: ['Video', 'Effects'],
        nodes: [
            { type: 'router', x: 50, y: 200 }, { type: 'analyst', x: 280, y: 120 },
            { type: 'analyst', x: 280, y: 300 }, { type: 'merger', x: 510, y: 200 },
            { type: 'scribe', x: 740, y: 200 },
        ],
        edges: [[0, 1], [0, 2], [1, 3], [2, 3], [3, 4]],
    },
    {
        key: 'fullstack', icon: '⭐',
        title_en: 'Full Stack Pro', title_zh: '全栈Pro',
        desc_en: 'Enterprise full-stack with analysis and documentation.',
        desc_zh: '企业级全栈项目，含分析和文档。',
        tags: ['Frontend', 'Backend', 'Docs'],
        nodes: [
            { type: 'router', x: 50, y: 250 }, { type: 'planner', x: 240, y: 250 },
            { type: 'reviewer', x: 430, y: 250 }, { type: 'builder', x: 620, y: 80 },
            { type: 'builder', x: 620, y: 250 }, { type: 'tester', x: 620, y: 420 },
            { type: 'analyst', x: 810, y: 80 }, { type: 'scribe', x: 810, y: 420 },
            { type: 'reviewer', x: 1000, y: 250 }, { type: 'deployer', x: 1190, y: 250 },
        ],
        edges: [[0, 1], [1, 2], [2, 3], [2, 4], [2, 5], [3, 6], [5, 7], [4, 8], [6, 8], [7, 8], [8, 9]],
    },
];

interface TemplateGalleryProps {
    open: boolean;
    onClose: () => void;
    onLoadTemplate: (tpl: TemplateDef) => void;
    lang: 'en' | 'zh';
}

export default function TemplateGallery({ open, onClose, onLoadTemplate, lang }: TemplateGalleryProps) {
    if (!open) return null;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-container modal-wide" onClick={e => e.stopPropagation()}>
                <div className="modal-header">
                    <h3>📂 {lang === 'zh' ? '模板库' : 'Template Gallery'}</h3>
                    <button className="modal-close" onClick={onClose}>✕</button>
                </div>
                <div className="modal-body">
                    <div className="tpl-grid">
                        {TEMPLATES.map(tpl => (
                            <div
                                key={tpl.key}
                                className="tpl-card"
                                onClick={() => { onLoadTemplate(tpl); onClose(); }}
                            >
                                <div className="tpl-icon">{tpl.icon}</div>
                                <div className="tpl-title">{lang === 'zh' ? tpl.title_zh : tpl.title_en}</div>
                                <div className="tpl-desc">{lang === 'zh' ? tpl.desc_zh : tpl.desc_en}</div>
                                <div className="tpl-flow">
                                    {tpl.nodes.map((n, i) => (
                                        <span key={i}>
                                            {i > 0 && ' → '}
                                            {n.type}
                                        </span>
                                    ))}
                                </div>
                                <div className="tpl-tags">
                                    {tpl.tags.map(tag => (
                                        <span key={tag} className="tpl-tag">{tag}</span>
                                    ))}
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
}
