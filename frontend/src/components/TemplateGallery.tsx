'use client';

import { useEffect, useState } from 'react';

const TEMPLATE_API_BASE =
    typeof window !== 'undefined' && (window as any).__EVERMIND_API_BASE__
        ? (window as any).__EVERMIND_API_BASE__
        : 'http://127.0.0.1:8765';

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
    /**
     * v6.2 (maintainer): optional one-click goal. When set, clicking
     * the card pre-fills the chat input with this goal so the user can run
     * immediately after loading the canvas. Quick-start templates rely on
     * this; classic workflow templates leave it unset.
     */
    goal?: string;
    /** Category tag for filtering: 'workflow' (node graph templates) vs 'quickstart' (goal + graph) vs 'user' (saved). */
    category?: 'workflow' | 'quickstart' | 'user';
    /** Rough time estimate shown on quick-start cards (seconds). */
    est_duration_sec?: number;
    cover_emoji?: string;
    /** v7.2: backend slug for user-saved templates (lets us delete/export). */
    user_slug?: string;
}

/** v7.2: convert backend `/api/templates/user` payload (template-shape:
 *  {key,label,task,depends_on}) into a canvas-shape TemplateDef. We auto-lay
 *  the nodes left-to-right grouped by depth so the user sees a sensible
 *  graph immediately. */
function userTemplateToDef(raw: any): TemplateDef | null {
    if (!raw || typeof raw !== 'object') return null;
    const nodes = Array.isArray(raw.nodes) ? raw.nodes : [];
    if (!nodes.length) return null;
    // Compute depth per node from depends_on graph
    const keyToIdx: Record<string, number> = {};
    nodes.forEach((n: any, i: number) => {
        const k = String(n?.key || `node${i}`);
        keyToIdx[k] = i;
    });
    const depth: number[] = nodes.map(() => 0);
    let changed = true;
    let safety = 16;
    while (changed && safety-- > 0) {
        changed = false;
        nodes.forEach((n: any, i: number) => {
            const deps: string[] = Array.isArray(n?.depends_on) ? n.depends_on : [];
            for (const d of deps) {
                const di = keyToIdx[d];
                if (di === undefined) continue;
                if (depth[di] + 1 > depth[i]) {
                    depth[i] = depth[di] + 1;
                    changed = true;
                }
            }
        });
    }
    const cols: number[][] = [];
    depth.forEach((d, i) => {
        cols[d] = cols[d] || [];
        cols[d].push(i);
    });
    const canvasNodes = nodes.map((n: any, i: number) => {
        const d = depth[i] || 0;
        const col = cols[d] || [];
        const row = col.indexOf(i);
        // v7.34: strip dedupe-suffix (`builder2` → `builder`) for NODE_TYPES.
        const rawKey = String(n?.key || 'agent').toLowerCase();
        const role = rawKey.replace(/\d+$/, '') || rawKey;
        // v7.39 (maintainer): use the saved x/y when present so the
        // user's exact canvas layout is restored. Fall back to auto-layout
        // (depth × 220, row × 130) only for older templates that pre-date
        // position persistence.
        const savedX = (typeof n?.x === 'number' && Number.isFinite(n.x)) ? Number(n.x) : null;
        const savedY = (typeof n?.y === 'number' && Number.isFinite(n.y)) ? Number(n.y) : null;
        return {
            type: role,
            x: savedX !== null ? savedX : 50 + d * 220,
            y: savedY !== null ? savedY : 100 + row * 130,
        };
    });
    // Edges from depends_on
    const edges: [number, number][] = [];
    nodes.forEach((n: any, i: number) => {
        const deps: string[] = Array.isArray(n?.depends_on) ? n.depends_on : [];
        for (const d of deps) {
            const di = keyToIdx[d];
            if (di !== undefined) edges.push([di, i]);
        }
    });
    const slug = String(raw.id || '').replace(/^user-/, '');
    const title = String(raw.name || slug || 'Untitled');
    const desc = String(raw.description || '').slice(0, 120) || 'User-saved template.';
    return {
        key: `user-${slug}`,
        icon: 'USR',
        title_en: title,
        title_zh: title,
        desc_en: desc,
        desc_zh: desc,
        tags: Array.isArray(raw.tags) ? raw.tags.slice(0, 4) : ['User'],
        nodes: canvasNodes,
        edges,
        category: 'user',
        cover_emoji: '⭐',
        user_slug: slug,
    };
}

// v7.34 (maintainer): WORKFLOW TEMPLATE OVERHAUL.
// Previous templates referenced node types like `screenshot`, `localshell`,
// `gitops`, `bgremove`, `videoedit` that have NO AGENT_PRESETS entry in
// ai_bridge.py — running them would fall through to a generic harness with
// no real behavior. Also missing `patcher` from every pipeline despite the
// v6.4 architecture requiring `reviewer→patcher→reviewer` for repair.
//
// New rules:
//  - Only use the 14 backend-supported roles: planner / analyst / uidesign /
//    scribe / imagegen / spritesheet / assetimport / builder / merger /
//    polisher / reviewer / patcher / debugger / deployer / tester.
//  - Every pipeline that includes `reviewer` ALSO includes `patcher` so the
//    repair loop can fire (v6.4 surgical edits).
//  - Pro/heavy templates use multiple builders + merger + polisher.
//  - All templates start with `planner` (router is supported but optional).
export const TEMPLATES: TemplateDef[] = [
    {
        key: 'webdev', icon: 'WEB',
        title_en: 'Web Development', title_zh: 'Web 开发',
        desc_en: 'Modern web app with parallel builders + repair loop.',
        desc_zh: '现代 Web 应用，双 builder 并行 + reviewer→patcher 修复闭环。',
        tags: ['Frontend', 'Web', 'Repair Loop'],
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'analyst', x: 240, y: 200 },
            { type: 'uidesign', x: 430, y: 200 },  { type: 'builder', x: 620, y: 100 },
            { type: 'builder', x: 620, y: 300 },   { type: 'merger', x: 810, y: 200 },
            { type: 'polisher', x: 1000, y: 200 }, { type: 'reviewer', x: 1190, y: 200 },
            { type: 'patcher', x: 1380, y: 200 },  { type: 'deployer', x: 1570, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3],[2,4],[3,5],[4,5],[5,6],[6,7],[7,8],[8,9]],
    },
    {
        key: 'artpipe', icon: 'ART',
        title_en: 'Art Asset Pipeline', title_zh: '美术资产管线',
        desc_en: 'Image generation → spritesheet → asset import for game art.',
        desc_zh: '游戏美术管线：图像生成 → 精灵图打包 → 资产导入。',
        tags: ['ImageGen', 'Sprites', 'Game Art'],
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'analyst', x: 240, y: 200 },
            { type: 'imagegen', x: 430, y: 80 },   { type: 'imagegen', x: 430, y: 200 },
            { type: 'imagegen', x: 430, y: 320 },  { type: 'spritesheet', x: 620, y: 200 },
            { type: 'assetimport', x: 810, y: 200 },
        ],
        edges: [[0,1],[1,2],[1,3],[1,4],[2,5],[3,5],[4,5],[5,6]],
    },
    {
        key: 'bugfix', icon: 'BUG',
        title_en: 'Bug Fix Loop', title_zh: '缺陷修复闭环',
        desc_en: 'Analyze → patch → review → re-patch until reviewer approves.',
        desc_zh: '分析 → 补丁 → 审查 → 再补丁，直到 reviewer 批准。',
        tags: ['Debug', 'Patch', 'Repair Loop'],
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'analyst', x: 240, y: 200 },
            { type: 'debugger', x: 430, y: 200 },  { type: 'patcher', x: 620, y: 200 },
            { type: 'reviewer', x: 810, y: 200 },  { type: 'deployer', x: 1000, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3],[3,4],[4,5]],
    },
    {
        key: 'docpipe', icon: 'DOC',
        title_en: 'Documentation Pipeline', title_zh: '文档生成管线',
        desc_en: 'Multi-source research → merge → narrative writing.',
        desc_zh: '多源调研合并 → 文案撰写,适合白皮书/教程/产品文档。',
        tags: ['Docs', 'Research', 'Writing'],
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'analyst', x: 240, y: 100 },
            { type: 'analyst', x: 240, y: 300 },   { type: 'merger', x: 430, y: 200 },
            { type: 'scribe', x: 620, y: 200 },    { type: 'reviewer', x: 810, y: 200 },
        ],
        edges: [[0,1],[0,2],[1,3],[2,3],[3,4],[4,5]],
    },
    {
        key: 'fullstack', icon: 'PRO',
        title_en: 'Full Stack Pro', title_zh: '全栈 Pro',
        desc_en: 'Heavy pipeline: analyst → ui → 2 builders → merger → polisher → review→patch→ship.',
        desc_zh: '重型管线：分析 → UI 设计 → 双 builder → 合并 → 抛光 → 审查/补丁 → 部署。',
        tags: ['Frontend', 'Backend', 'Premium'],
        nodes: [
            { type: 'planner', x: 50, y: 250 },    { type: 'analyst', x: 230, y: 250 },
            { type: 'uidesign', x: 410, y: 250 },  { type: 'scribe', x: 590, y: 80 },
            { type: 'builder', x: 590, y: 250 },   { type: 'builder', x: 590, y: 420 },
            { type: 'merger', x: 770, y: 250 },    { type: 'polisher', x: 950, y: 250 },
            { type: 'reviewer', x: 1130, y: 250 }, { type: 'patcher', x: 1310, y: 250 },
            { type: 'deployer', x: 1490, y: 250 },
        ],
        edges: [[0,1],[1,2],[2,3],[2,4],[2,5],[4,6],[5,6],[3,7],[6,7],[7,8],[8,9],[9,10]],
    },
    // v5.8.6: three new curated starter templates tuned to Evermind's real node economics.
    // Each template is self-contained (fills canvas + runnable end-to-end).
    {
        key: 'landing', icon: 'LAND',
        title_en: 'Quick Landing Page', title_zh: '单页落地页',
        desc_en: 'Fast single-page marketing site with repair loop.',
        desc_zh: '快速单页营销站，含 reviewer→patcher 修复闭环。',
        tags: ['Landing', 'Marketing', 'Fast'],
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'builder', x: 240, y: 200 },
            { type: 'reviewer', x: 430, y: 200 },  { type: 'patcher', x: 620, y: 200 },
            { type: 'deployer', x: 810, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3],[3,4]],
    },
    {
        key: 'game3d', icon: '3D',
        title_en: '3D Game (Premium)', title_zh: '3D 游戏 (高质量)',
        desc_en: 'Three.js game with asset pipeline + 2 builders + repair loop.',
        desc_zh: 'Three.js 完整 3D 游戏,资产管线 + 双 builder + 修复闭环。',
        tags: ['3D', 'Three.js', 'Game', 'Premium'],
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'analyst', x: 230, y: 80 },
            { type: 'imagegen', x: 230, y: 200 },  { type: 'spritesheet', x: 230, y: 320 },
            { type: 'assetimport', x: 410, y: 320 },
            { type: 'builder', x: 600, y: 100 },   { type: 'builder', x: 600, y: 300 },
            { type: 'merger', x: 790, y: 200 },    { type: 'polisher', x: 980, y: 200 },
            { type: 'reviewer', x: 1170, y: 200 }, { type: 'patcher', x: 1360, y: 200 },
            { type: 'debugger', x: 1550, y: 200 }, { type: 'deployer', x: 1740, y: 200 },
        ],
        edges: [
            [0,1],[0,2],[0,3],[2,4],[3,4],
            [1,5],[4,5],[1,6],[4,6],
            [5,7],[6,7],[7,8],[8,9],[9,10],[10,11],[11,12],
        ],
    },
    {
        key: 'dashboard', icon: 'DASH',
        title_en: 'Data Dashboard', title_zh: '数据仪表盘',
        desc_en: '3 parallel widget builders + merger + polisher + repair loop.',
        desc_zh: '3 个并行 widget builder + 合并 + 抛光 + 修复闭环,适合复杂仪表盘。',
        tags: ['Dashboard', 'Charts', 'Parallel'],
        nodes: [
            { type: 'planner', x: 50, y: 250 },    { type: 'analyst', x: 230, y: 250 },
            { type: 'uidesign', x: 410, y: 250 },
            { type: 'builder', x: 590, y: 80 },    { type: 'builder', x: 590, y: 250 },
            { type: 'builder', x: 590, y: 420 },
            { type: 'merger', x: 770, y: 250 },    { type: 'polisher', x: 950, y: 250 },
            { type: 'reviewer', x: 1130, y: 250 }, { type: 'patcher', x: 1310, y: 250 },
            { type: 'deployer', x: 1490, y: 250 },
        ],
        edges: [
            [0,1],[1,2],[2,3],[2,4],[2,5],
            [3,6],[4,6],[5,6],[6,7],[7,8],[8,9],[9,10],
        ],
    },

    // ─── Quick-Start Templates ───
    // Goal+graph pairs. Clicking pre-fills the chat input with `goal`. Every
    // pipeline that includes `reviewer` ALSO includes `patcher` so reviewer
    // rejections can be surgically repaired (v6.4 architecture).
    {
        key: 'qs-2d-snake', icon: '🐍', category: 'quickstart',
        title_en: '2D Snake Game', title_zh: '2D 贪吃蛇小游戏',
        desc_en: 'Classic snake with arrow keys and score counter.',
        desc_zh: '经典玩法 + 上下左右 + 分数计数。',
        tags: ['Game', '2D', 'Easy'],
        cover_emoji: '🐍', est_duration_sec: 180,
        goal: 'Build a polished 2D Snake game in a single HTML5 page. WASD or arrow keys move the snake; eating food increases length and score; wall/self collision ends the game with a restart prompt.',
        nodes: [
            { type: 'planner', x: 50, y: 200 },   { type: 'builder', x: 240, y: 200 },
            { type: 'reviewer', x: 430, y: 200 }, { type: 'patcher', x: 620, y: 200 },
            { type: 'deployer', x: 810, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3],[3,4]],
    },
    {
        key: 'qs-landing-saas', icon: '🚀', category: 'quickstart',
        title_en: 'SaaS Landing Page', title_zh: 'SaaS 产品落地页',
        desc_en: 'Hero + features + pricing + CTA with motion.',
        desc_zh: 'Hero + 功能网格 + 定价 + CTA。',
        tags: ['Landing', 'Marketing'],
        cover_emoji: '🚀', est_duration_sec: 240,
        goal: "Build a modern SaaS landing page for a fictional project management tool 'Orbit'. Sections: hero with tagline and primary CTA, three-feature grid with icons, testimonials carousel, three-tier pricing, FAQ accordion, footer. Use restrained motion and a soft purple/blue palette.",
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'uidesign', x: 240, y: 200 },
            { type: 'builder', x: 430, y: 200 },   { type: 'polisher', x: 620, y: 200 },
            { type: 'reviewer', x: 810, y: 200 },  { type: 'patcher', x: 1000, y: 200 },
            { type: 'deployer', x: 1190, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3],[3,4],[4,5],[5,6]],
    },
    {
        key: 'qs-analytics-dashboard', icon: '📊', category: 'quickstart',
        title_en: 'Analytics Dashboard', title_zh: '数据分析仪表盘',
        desc_en: 'KPI cards + line chart + data table.',
        desc_zh: 'KPI 卡 + 折线图 + 数据表。',
        tags: ['Dashboard', 'Charts'],
        cover_emoji: '📊', est_duration_sec: 300,
        goal: 'Build an analytics dashboard for a fictional e-commerce store. Include: sidebar nav, 4 KPI stat cards (revenue, orders, customers, AOV), a line chart using Chart.js (last 30 days), a sortable data table of recent orders, a theme toggle. Use mock data inline.',
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'analyst', x: 240, y: 200 },
            { type: 'builder', x: 430, y: 200 },   { type: 'reviewer', x: 620, y: 200 },
            { type: 'patcher', x: 810, y: 200 },   { type: 'deployer', x: 1000, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3],[3,4],[4,5]],
    },
    {
        key: 'qs-slides-pitch', icon: '📽️', category: 'quickstart',
        title_en: 'Pitch Deck Slides', title_zh: '创业路演幻灯片',
        desc_en: '10-slide Reveal.js pitch deck.',
        desc_zh: 'Reveal.js 10 页幻灯片。',
        tags: ['Slides', 'Presentation'],
        cover_emoji: '📽️', est_duration_sec: 240,
        goal: 'Build a 10-slide pitch deck using Reveal.js for a fictional climate-tech startup. Slides: title, problem, solution, market size, product demo placeholder, traction metrics, team, ask (funding round), contact, thank you. Arrow keys navigate, with a subtle progress bar.',
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'builder', x: 240, y: 200 },
            { type: 'reviewer', x: 430, y: 200 },  { type: 'patcher', x: 620, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3]],
    },
    {
        key: 'qs-todo-pro', icon: '✅', category: 'quickstart',
        title_en: 'Todo with Categories', title_zh: '任务清单（带分类）',
        desc_en: 'Add/complete/delete with localStorage.',
        desc_zh: '添加/完成/删除 + 本地持久化。',
        tags: ['Tool', 'Easy'],
        cover_emoji: '✅', est_duration_sec: 150,
        goal: 'Build a to-do list web app with add/edit/complete/delete, category filters (work/personal/other), localStorage persistence, and a dark mode toggle. Use clean typography and subtle hover states.',
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'builder', x: 240, y: 200 },
            { type: 'reviewer', x: 430, y: 200 },  { type: 'patcher', x: 620, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3]],
    },
    {
        key: 'qs-particle-webgl', icon: '✨', category: 'quickstart',
        title_en: 'WebGL Particle Field', title_zh: 'WebGL 粒子场',
        desc_en: '2000+ particles with mouse attraction.',
        desc_zh: '2000+ 粒子 + 鼠标吸引。',
        tags: ['Creative', 'WebGL', 'Hard'],
        cover_emoji: '✨', est_duration_sec: 360,
        goal: 'Build a fullscreen WebGL particle visualization using Three.js. 2000+ particles form a slowly rotating torus. Mouse movement attracts nearby particles. Color palette: deep navy background with cyan/magenta particles. Include a small HUD with FPS counter.',
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'analyst', x: 240, y: 200 },
            { type: 'builder', x: 430, y: 200 },   { type: 'reviewer', x: 620, y: 200 },
            { type: 'patcher', x: 810, y: 200 },   { type: 'debugger', x: 1000, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3],[3,4],[4,5]],
    },
    {
        key: 'qs-form-contact', icon: '✉️', category: 'quickstart',
        title_en: 'Contact Form (Live Validation)', title_zh: '联系表单（实时校验）',
        desc_en: 'Field-level validation + success animation.',
        desc_zh: '字段校验 + 成功动画。',
        tags: ['Tool', 'Easy'],
        cover_emoji: '✉️', est_duration_sec: 120,
        goal: 'Build a contact form with real-time field validation (name, email, message). Show inline error messages under each field. On successful submit, show a checkmark animation and clear the form. No backend call — simulate success after 1s.',
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'builder', x: 240, y: 200 },
            { type: 'reviewer', x: 430, y: 200 },  { type: 'patcher', x: 620, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3]],
    },
    {
        key: 'qs-portfolio-clone', icon: '🎨', category: 'quickstart',
        title_en: 'Designer Portfolio', title_zh: '设计师作品集主页',
        desc_en: 'Big headline + project grid + about.',
        desc_zh: '大字标题 + 项目网格 + 关于。',
        tags: ['Portfolio', 'Website'],
        cover_emoji: '🎨', est_duration_sec: 240,
        goal: 'Build a modern designer portfolio: bold oversized hero headline with name and tagline, scroll-triggered project grid (6 placeholder projects with hover reveal), about section with skills chips, contact strip. Use serif headlines paired with sans-serif body. Subtle parallax on hero.',
        nodes: [
            { type: 'planner', x: 50, y: 200 },    { type: 'uidesign', x: 240, y: 200 },
            { type: 'builder', x: 430, y: 200 },   { type: 'polisher', x: 620, y: 200 },
            { type: 'reviewer', x: 810, y: 200 },  { type: 'patcher', x: 1000, y: 200 },
        ],
        edges: [[0,1],[1,2],[2,3],[3,4],[4,5]],
    },
];

interface TemplateGalleryProps {
    open: boolean;
    onClose: () => void;
    onLoadTemplate: (tpl: TemplateDef) => void;
    lang: 'en' | 'zh';
    /** v7.2: snapshot of the current canvas (passed in by editor) so the
     *  gallery can offer a "Save Current as Template..." action. The gallery
     *  serializes nodes/edges → POST /api/templates/user. If undefined, the
     *  Save button is hidden. */
    currentCanvas?: {
        nodes: { id?: string; type: string; x: number; y: number; data?: any }[];
        edges: [number, number][];
    };
}

/** Quick-start templates (category='quickstart') have a `goal` string that
 *  can pre-fill the chat input. Workflow templates are pure node graphs. */
export function getQuickStartTemplates(): TemplateDef[] {
    return TEMPLATES.filter(t => t.category === 'quickstart');
}

// v7.4: Electron 18+ disables window.prompt/confirm by default — using
// them silently returns null, so the previous "Save Current as Template"
// button looked like nothing happened when clicked. Replace with a
// promise-based inline modal so the same code path works in browser AND
// Electron.
type PromptState = {
    open: boolean;
    title: string;
    defaultValue: string;
    multiline?: boolean;
    isConfirm?: boolean;
    placeholder?: string;
    resolve: ((v: string | null) => void) | null;
};

export default function TemplateGallery({ open, onClose, onLoadTemplate, lang, currentCanvas }: TemplateGalleryProps) {
    const [filter, setFilter] = useState<'all' | 'quickstart' | 'workflow' | 'user'>('all');
    const [userTemplates, setUserTemplates] = useState<TemplateDef[]>([]);
    const [busy, setBusy] = useState(false);
    const [refreshKey, setRefreshKey] = useState(0);
    const [promptState, setPromptState] = useState<PromptState>({
        open: false, title: '', defaultValue: '', resolve: null,
    });
    const [promptValue, setPromptValue] = useState('');

    const askPrompt = (title: string, defaultValue = '', opts: { multiline?: boolean; placeholder?: string } = {}): Promise<string | null> => {
        return new Promise(resolve => {
            setPromptValue(defaultValue);
            setPromptState({
                open: true,
                title,
                defaultValue,
                multiline: !!opts.multiline,
                placeholder: opts.placeholder,
                isConfirm: false,
                resolve,
            });
        });
    };
    const askConfirm = (title: string): Promise<boolean> => {
        return new Promise(resolve => {
            setPromptState({
                open: true,
                title,
                defaultValue: '',
                isConfirm: true,
                resolve: (v: string | null) => resolve(v !== null),
            });
        });
    };
    const closePrompt = (value: string | null) => {
        const r = promptState.resolve;
        setPromptState(s => ({ ...s, open: false, resolve: null }));
        if (r) r(value);
    };

    // v7.2: load user-saved templates when gallery opens or after a save/delete
    useEffect(() => {
        if (!open) return;
        const ctrl = new AbortController();
        (async () => {
            try {
                const r = await fetch(`${TEMPLATE_API_BASE}/api/templates/user`, { signal: ctrl.signal });
                if (!r.ok) return;
                const j = await r.json();
                const list: any[] = Array.isArray(j?.templates) ? j.templates : [];
                const mapped = list
                    .map(userTemplateToDef)
                    .filter((x): x is TemplateDef => Boolean(x));
                setUserTemplates(mapped);
            } catch {
                /* ignore offline / cors */
            }
        })();
        return () => ctrl.abort();
    }, [open, refreshKey]);

    if (!open) return null;

    const allTemplates = [...TEMPLATES, ...userTemplates];
    const visible = filter === 'all'
        ? allTemplates
        : allTemplates.filter(t => (t.category || 'workflow') === filter);

    // v7.2: serialize current canvas → backend template payload + POST
    const handleSaveCurrent = async () => {
        if (!currentCanvas || !currentCanvas.nodes.length) {
            alert(lang === 'zh' ? '画布为空，无法保存。' : 'Canvas is empty.');
            return;
        }
        const name = await askPrompt(
            lang === 'zh' ? '模板名称：' : 'Template name:',
            lang === 'zh' ? '我的模板' : 'My Template',
            { placeholder: lang === 'zh' ? '给模板取个名字' : 'Name your template' },
        );
        if (!name) return;
        const description = (await askPrompt(
            lang === 'zh' ? '模板描述（可选）：' : 'Description (optional):',
            '',
            { multiline: true, placeholder: lang === 'zh' ? '可选：简单描述这个模板的用途' : 'Optional: short description' },
        )) || '';
        // Convert canvas (typed nodes + index edges) → backend template (key/depends_on)
        // v7.34 (maintainer): the React Flow `type` field is ALWAYS the
        // string `'agent'` (the component-type registered in nodeTypes={agent: AgentNode}).
        // The real agent role (planner/builder/...) lives in `data.nodeType`.
        // Reading `n.type` here always produced `'agent'`, so saved user
        // templates ended up with key='agent' / 'agent2' / 'agent3' and the
        // canvas reload showed all nodes labeled "agent". Read from
        // `data.nodeType` (or `data.agent`) first; fall back to `n.type`
        // only if both data fields are missing.
        const idxToKey: Record<number, string> = {};
        const usedKeys: Record<string, number> = {};
        const tplNodes = currentCanvas.nodes.map((n, i) => {
            const role = String(
                n.data?.nodeType
                || n.data?.nodeKey
                || n.data?.agent
                || n.type
                || 'agent'
            ).toLowerCase();
            // De-duplicate keys (e.g. two builders → builder, builder2)
            const ct = (usedKeys[role] || 0) + 1;
            usedKeys[role] = ct;
            const key = ct === 1 ? role : `${role}${ct}`;
            idxToKey[i] = key;
            // v7.39 (maintainer): persist x/y so reload restores the
            // exact canvas layout the user had when saving. Without this the
            // backend dropped position and frontend auto-relayed by depth,
            // making custom arrangements feel "lost" after save+reload.
            return {
                key,
                label: String(n.data?.label || role || key),
                task: String(n.data?.task || ''),
                depends_on: [] as string[],
                x: Number(n.x ?? 0),
                y: Number(n.y ?? 0),
            };
        });
        for (const [from, to] of currentCanvas.edges) {
            if (tplNodes[to]) {
                const dep = idxToKey[from];
                if (dep && !tplNodes[to].depends_on.includes(dep)) {
                    tplNodes[to].depends_on.push(dep);
                }
            }
        }
        setBusy(true);
        try {
            const r = await fetch(`${TEMPLATE_API_BASE}/api/templates/user`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, description, nodes: tplNodes }),
            });
            const j = await r.json();
            if (!r.ok || !j.ok) throw new Error(j.error || 'save failed');
            setRefreshKey(k => k + 1);
            alert(lang === 'zh' ? `已保存模板：${name}` : `Saved: ${name}`);
        } catch (e: any) {
            alert((lang === 'zh' ? '保存失败：' : 'Save failed: ') + (e?.message || String(e)));
        } finally {
            setBusy(false);
        }
    };

    // v7.2: delete a user-saved template
    const handleDeleteUser = async (slug: string, name: string) => {
        const ok = await askConfirm(lang === 'zh' ? `删除模板 "${name}"？` : `Delete template "${name}"?`);
        if (!ok) return;
        try {
            const r = await fetch(`${TEMPLATE_API_BASE}/api/templates/user/${encodeURIComponent(slug)}`, {
                method: 'DELETE',
            });
            if (!r.ok) throw new Error('delete failed');
            setRefreshKey(k => k + 1);
        } catch (e: any) {
            alert((lang === 'zh' ? '删除失败：' : 'Delete failed: ') + (e?.message || String(e)));
        }
    };

    // v7.2: export → clipboard
    const handleExportUser = async (slug: string) => {
        try {
            const r = await fetch(`${TEMPLATE_API_BASE}/api/templates/user/${encodeURIComponent(slug)}/export`);
            const j = await r.json();
            if (!j.ok) throw new Error(j.error || 'export failed');
            await navigator.clipboard.writeText(JSON.stringify(j.template, null, 2));
            alert(lang === 'zh' ? '模板 JSON 已复制到剪贴板。' : 'Template JSON copied to clipboard.');
        } catch (e: any) {
            alert((lang === 'zh' ? '导出失败：' : 'Export failed: ') + (e?.message || String(e)));
        }
    };

    // v7.2: import from pasted JSON
    const handleImport = async () => {
        const text = await askPrompt(
            lang === 'zh' ? '粘贴模板 JSON：' : 'Paste template JSON:',
            '',
            { multiline: true, placeholder: '{"name":"...","nodes":[...]}' },
        );
        if (!text) return;
        try {
            const parsed = JSON.parse(text);
            const r = await fetch(`${TEMPLATE_API_BASE}/api/templates/user/import`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ template: parsed }),
            });
            const j = await r.json();
            if (!r.ok || !j.ok) throw new Error(j.error || 'import failed');
            setRefreshKey(k => k + 1);
            alert(lang === 'zh' ? '导入成功。' : 'Import successful.');
        } catch (e: any) {
            alert((lang === 'zh' ? '导入失败：' : 'Import failed: ') + (e?.message || String(e)));
        }
    };

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-container modal-wide" onClick={e => e.stopPropagation()}>
                <div className="modal-header">
                    <h3>{lang === 'zh' ? '模板库' : 'Template Gallery'}</h3>
                    <div style={{ display: 'flex', gap: 8, marginLeft: 'auto', marginRight: 16, alignItems: 'center', flexWrap: 'wrap' }}>
                        {(['all', 'quickstart', 'workflow', 'user'] as const).map(f => (
                            <button
                                key={f}
                                onClick={() => setFilter(f)}
                                style={{
                                    padding: '4px 12px', borderRadius: 6, fontSize: 11,
                                    border: filter === f ? '1px solid rgba(168,85,247,0.5)' : '1px solid rgba(255,255,255,0.1)',
                                    background: filter === f ? 'rgba(168,85,247,0.18)' : 'transparent',
                                    color: filter === f ? '#d4a8ff' : 'var(--text2)',
                                    cursor: 'pointer',
                                }}
                            >
                                {f === 'all'
                                    ? (lang === 'zh' ? '全部' : 'All')
                                    : f === 'quickstart'
                                        ? (lang === 'zh' ? '⚡ 一键开始' : '⚡ Quick Start')
                                        : f === 'workflow'
                                            ? (lang === 'zh' ? '🧩 工作流' : '🧩 Workflow')
                                            : (lang === 'zh' ? '⭐ 我的' : '⭐ My')}
                            </button>
                        ))}
                        {/* v7.2: actions */}
                        <span style={{ width: 1, height: 18, background: 'rgba(255,255,255,0.1)', margin: '0 4px' }} />
                        {currentCanvas && (
                            <button
                                onClick={handleSaveCurrent}
                                disabled={busy}
                                title={lang === 'zh' ? '把当前画布保存为模板' : 'Save current canvas as a template'}
                                style={{
                                    padding: '4px 10px', borderRadius: 6, fontSize: 11,
                                    border: '1px solid rgba(120,180,255,0.45)',
                                    background: busy ? 'rgba(120,180,255,0.08)' : 'rgba(120,180,255,0.15)',
                                    color: '#a8c8ff', cursor: busy ? 'wait' : 'pointer',
                                }}
                            >
                                {busy ? '...' : (lang === 'zh' ? '＋ 保存当前' : '＋ Save Current')}
                            </button>
                        )}
                        <button
                            onClick={handleImport}
                            title={lang === 'zh' ? '从 JSON 导入' : 'Import from JSON'}
                            style={{
                                padding: '4px 10px', borderRadius: 6, fontSize: 11,
                                border: '1px solid rgba(255,255,255,0.1)',
                                background: 'transparent', color: 'var(--text2)', cursor: 'pointer',
                            }}
                        >
                            {lang === 'zh' ? '↓ 导入' : '↓ Import'}
                        </button>
                    </div>
                    <button className="modal-close" onClick={onClose}>✕</button>
                </div>
                <div className="modal-body">
                    <div className="tpl-grid">
                        {visible.map(tpl => {
                            const isQuickStart = tpl.category === 'quickstart';
                            const isUser = tpl.category === 'user';
                            return (
                                <div
                                    key={tpl.key}
                                    className="tpl-card"
                                    onClick={() => { onLoadTemplate(tpl); onClose(); }}
                                    style={{
                                        position: 'relative',
                                        ...(isQuickStart ? { borderLeft: '3px solid rgba(168,85,247,0.6)' } : {}),
                                        ...(isUser ? { borderLeft: '3px solid rgba(120,180,255,0.55)' } : {}),
                                    }}
                                >
                                    {isUser && tpl.user_slug && (
                                        <div
                                            style={{
                                                position: 'absolute', top: 8, right: 8, display: 'flex', gap: 4,
                                            }}
                                            onClick={e => e.stopPropagation()}
                                        >
                                            <button
                                                onClick={() => handleExportUser(tpl.user_slug!)}
                                                title={lang === 'zh' ? '导出 JSON' : 'Export JSON'}
                                                style={{
                                                    padding: '2px 6px', fontSize: 10, borderRadius: 4,
                                                    border: '1px solid rgba(255,255,255,0.12)',
                                                    background: 'rgba(0,0,0,0.3)', color: 'var(--text2)',
                                                    cursor: 'pointer',
                                                }}
                                            >
                                                ↗
                                            </button>
                                            <button
                                                onClick={() => handleDeleteUser(tpl.user_slug!, tpl.title_en)}
                                                title={lang === 'zh' ? '删除' : 'Delete'}
                                                style={{
                                                    padding: '2px 6px', fontSize: 10, borderRadius: 4,
                                                    border: '1px solid rgba(255,90,90,0.25)',
                                                    background: 'rgba(0,0,0,0.3)', color: '#f87171',
                                                    cursor: 'pointer',
                                                }}
                                            >
                                                ✕
                                            </button>
                                        </div>
                                    )}
                                    <div className="tpl-icon">{tpl.cover_emoji || tpl.icon}</div>
                                    <div className="tpl-title">
                                        {lang === 'zh' ? tpl.title_zh : tpl.title_en}
                                        {isQuickStart && (
                                            <span style={{
                                                marginLeft: 6, fontSize: 9, color: '#d4a8ff',
                                                background: 'rgba(168,85,247,0.15)', padding: '1px 6px',
                                                borderRadius: 3, fontWeight: 500, verticalAlign: 'middle',
                                            }}>⚡ {lang === 'zh' ? '一键' : 'Quick'}</span>
                                        )}
                                    </div>
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
                                        {tpl.est_duration_sec && (
                                            <span className="tpl-tag" style={{ opacity: 0.7 }}>
                                                ~{Math.round(tpl.est_duration_sec / 60)} min
                                            </span>
                                        )}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
            </div>
            {promptState.open && (
                <div
                    onClick={() => closePrompt(null)}
                    style={{
                        position: 'fixed', inset: 0, zIndex: 10000,
                        background: 'rgba(0,0,0,0.55)',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                    }}
                >
                    <div
                        onClick={e => e.stopPropagation()}
                        style={{
                            width: 'min(480px, 90vw)',
                            background: 'var(--surface, #1a1620)',
                            border: '1px solid rgba(255,255,255,0.1)',
                            borderRadius: 10,
                            padding: '20px 22px',
                            boxShadow: '0 12px 48px rgba(0,0,0,0.5)',
                        }}
                    >
                        <div style={{ fontSize: 14, fontWeight: 500, color: 'var(--text)', marginBottom: 14 }}>
                            {promptState.title}
                        </div>
                        {!promptState.isConfirm && (
                            promptState.multiline ? (
                                <textarea
                                    value={promptValue}
                                    onChange={e => setPromptValue(e.target.value)}
                                    placeholder={promptState.placeholder || ''}
                                    autoFocus
                                    rows={5}
                                    style={{
                                        width: '100%', boxSizing: 'border-box',
                                        padding: '8px 10px',
                                        background: 'rgba(0,0,0,0.25)',
                                        border: '1px solid rgba(255,255,255,0.15)',
                                        borderRadius: 6,
                                        color: 'var(--text)', fontSize: 13,
                                        resize: 'vertical',
                                    }}
                                />
                            ) : (
                                <input
                                    type="text"
                                    value={promptValue}
                                    onChange={e => setPromptValue(e.target.value)}
                                    placeholder={promptState.placeholder || ''}
                                    autoFocus
                                    onKeyDown={e => {
                                        if (e.key === 'Enter') closePrompt(promptValue);
                                        if (e.key === 'Escape') closePrompt(null);
                                    }}
                                    style={{
                                        width: '100%', boxSizing: 'border-box',
                                        padding: '8px 10px',
                                        background: 'rgba(0,0,0,0.25)',
                                        border: '1px solid rgba(255,255,255,0.15)',
                                        borderRadius: 6,
                                        color: 'var(--text)', fontSize: 13,
                                    }}
                                />
                            )
                        )}
                        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 16 }}>
                            <button
                                onClick={() => closePrompt(null)}
                                style={{
                                    padding: '6px 14px', borderRadius: 6, fontSize: 12,
                                    border: '1px solid rgba(255,255,255,0.15)',
                                    background: 'transparent', color: 'var(--text2)',
                                    cursor: 'pointer',
                                }}
                            >
                                {lang === 'zh' ? '取消' : 'Cancel'}
                            </button>
                            <button
                                onClick={() => closePrompt(promptState.isConfirm ? '' : promptValue)}
                                style={{
                                    padding: '6px 14px', borderRadius: 6, fontSize: 12,
                                    border: '1px solid rgba(120,180,255,0.5)',
                                    background: 'rgba(120,180,255,0.2)', color: '#a8c8ff',
                                    cursor: 'pointer',
                                }}
                            >
                                {lang === 'zh' ? '确定' : 'OK'}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
