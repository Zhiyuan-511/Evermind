'use client';

import { useEffect, useMemo, useState } from 'react';

import { deleteSkill, installSkill, listSkills } from '@/lib/api';
import type { SkillLibraryRecord } from '@/lib/types';

interface SkillsLibraryModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
}

type CapabilityCard = {
    id: string;
    titleZh: string;
    titleEn: string;
    summaryZh: string;
    summaryEn: string;
    example: string;
    tags: string[];
};

const CAPABILITIES: CapabilityCard[] = [
    {
        id: 'website',
        titleZh: '品牌官网 / 落地页',
        titleEn: 'Website / Landing Page',
        summaryZh: '适合营销官网、产品页、活动页。会调用 UI、动效、转化相关技能。',
        summaryEn: 'For marketing sites, product pages, and launch pages. Uses UI, motion, and conversion skills.',
        example: '做一个高端 SaaS 品牌官网，带插画 hero、滚动动效和清晰 CTA。',
        tags: ['website', 'ui', 'motion'],
    },
    {
        id: 'dashboard',
        titleZh: '仪表盘 / Admin 后台',
        titleEn: 'Dashboard / Admin',
        summaryZh: '这不是单独页面按钮，而是一类任务类型。系统会自动调用仪表盘信息密度和图表清晰度技能。',
        summaryEn: 'This is a task archetype, not a standalone page. Evermind routes dashboard tasks to analytics-focused skills.',
        example: '做一个 B2B SaaS 仪表盘，带 sidebar、图表筛选和数据表格。',
        tags: ['dashboard', 'analytics', 'admin'],
    },
    {
        id: 'game',
        titleZh: '可玩游戏 / 原型',
        titleEn: 'Playable Game / Prototype',
        summaryZh: '强调先做可玩的核心循环，再由 reviewer/tester 真正试玩并打回问题。',
        summaryEn: 'Prioritizes a playable core loop first, then real gameplay QA and rejection if it is not actually playable.',
        example: '做一个能立即开始玩的 2D 飞机大战 vertical slice，包含分数和 Game Over。',
        tags: ['game', 'qa', 'loop'],
    },
    {
        id: 'presentation',
        titleZh: 'PPT / 路演材料',
        titleEn: 'PPT / Pitch Deck',
        summaryZh: '适合融资路演、汇报 deck、产品发布内容，偏结构化表达和导出桥接。',
        summaryEn: 'For pitch decks, reports, and product launch materials, with stronger narrative and export scaffolding.',
        example: '做一个 12 页融资路演 deck，含市场、产品、增长和商业模式。',
        tags: ['slides', 'ppt', 'deck'],
    },
    {
        id: 'video',
        titleZh: '宣传视频 / 分镜',
        titleEn: 'Promo Video / Storyboard',
        summaryZh: '现在已经支持视频类技能路由，适合做 scene list、镜头脚本、字幕和 continuity brief。',
        summaryEn: 'Video-oriented skills now support scene lists, shot prompts, captions, and continuity briefs.',
        example: '为 AI 产品做一个 30 秒宣传短片 storyboard，输出分镜、时长和镜头 prompt。',
        tags: ['video', 'storyboard', 'shot'],
    },
    {
        id: 'docs',
        titleZh: '文档 / README / API',
        titleEn: 'Docs / README / API',
        summaryZh: '文档类任务会走结构化写作、图解说明和清晰度技能。',
        summaryEn: 'Documentation tasks route into structured writing, diagram, and clarity skills.',
        example: '为这个项目写一套 README、快速开始和 API 集成文档。',
        tags: ['docs', 'api', 'guide'],
    },
];

function splitCsv(value: string): string[] {
    return value
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean);
}

export default function SkillsLibraryModal({ open, onClose, lang }: SkillsLibraryModalProps) {
    const tr = (zh: string, en: string) => (lang === 'zh' ? zh : en);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [skills, setSkills] = useState<SkillLibraryRecord[]>([]);
    const [search, setSearch] = useState('');
    const [installing, setInstalling] = useState(false);
    const [sourceUrl, setSourceUrl] = useState('');
    const [skillName, setSkillName] = useState('');
    const [nodeTypesInput, setNodeTypesInput] = useState('builder');
    const [keywordsInput, setKeywordsInput] = useState('');
    const [tagsInput, setTagsInput] = useState('');
    const [statusText, setStatusText] = useState('');

    const refresh = async () => {
        setLoading(true);
        setError('');
        try {
            const result = await listSkills();
            setSkills(Array.isArray(result.skills) ? result.skills : []);
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        if (!open) return;
        void refresh();
    }, [open]);

    const filteredSkills = useMemo(() => {
        const q = search.trim().toLowerCase();
        if (!q) return skills;
        return skills.filter((skill) => {
            const haystack = [
                skill.name,
                skill.title,
                skill.summary,
                skill.category,
                ...(skill.tags || []),
                ...(skill.keywords || []),
                ...(skill.node_types || []),
                skill.source_name,
            ].join(' ').toLowerCase();
            return haystack.includes(q);
        });
    }, [search, skills]);

    const builtinSkills = filteredSkills.filter((skill) => skill.origin === 'builtin');
    const communitySkills = filteredSkills.filter((skill) => skill.origin === 'community');

    const handleInstall = async () => {
        if (!sourceUrl.trim()) {
            setStatusText(tr('请先输入 GitHub 技能链接。', 'Please enter a GitHub skill URL.'));
            return;
        }
        setInstalling(true);
        setStatusText('');
        try {
            const result = await installSkill({
                source_url: sourceUrl.trim(),
                name: skillName.trim() || undefined,
                node_types: splitCsv(nodeTypesInput),
                keywords: splitCsv(keywordsInput),
                tags: splitCsv(tagsInput),
            });
            setStatusText(tr(`已安装技能：${result.skill.title}`, `Installed skill: ${result.skill.title}`));
            setSourceUrl('');
            setSkillName('');
            setKeywordsInput('');
            setTagsInput('');
            await refresh();
        } catch (err) {
            setStatusText(err instanceof Error ? err.message : String(err));
        } finally {
            setInstalling(false);
        }
    };

    const handleDelete = async (name: string) => {
        setStatusText('');
        try {
            await deleteSkill(name);
            setStatusText(tr(`已移除社区技能：${name}`, `Removed community skill: ${name}`));
            await refresh();
        } catch (err) {
            setStatusText(err instanceof Error ? err.message : String(err));
        }
    };

    const copyPrompt = async (value: string) => {
        try {
            await navigator.clipboard.writeText(value);
            setStatusText(tr('示例需求已复制到剪贴板。', 'Example prompt copied to clipboard.'));
        } catch {
            setStatusText(tr('复制失败，请手动复制。', 'Copy failed. Please copy manually.'));
        }
    };

    if (!open) return null;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-container modal-wide" onClick={(e) => e.stopPropagation()} style={{ width: 1180, maxWidth: '95vw' }}>
                <div className="modal-header">
                    <div>
                        <h3>{tr('技能库 / 资源库', 'Skills Library')}</h3>
                        <div style={{ fontSize: 12, color: 'var(--text3)', marginTop: 4 }}>
                            {tr('这里会显示内置技能、社区技能和任务能力类型。', 'Browse built-in skills, community installs, and task archetypes.')}
                        </div>
                    </div>
                    <button className="modal-close" onClick={onClose}>✕</button>
                </div>

                <div className="modal-body" style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: 14, minHeight: 620 }}>
                    <div style={{ display: 'grid', gap: 12, alignContent: 'start' }}>
                        <section style={{ border: '1px solid var(--glass-border)', borderRadius: 14, padding: 14, background: 'rgba(255,255,255,0.03)' }}>
                            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>{tr('社区技能导入', 'Community Skill Import')}</div>
                            <div style={{ fontSize: 11, color: 'var(--text3)', marginBottom: 10 }}>
                                {tr('支持公开 GitHub skill 文件夹或直接指向 SKILL.md 的链接。安装后会进入 Evermind 技能库并参与关键词触发。', 'Supports public GitHub skill folders or direct SKILL.md links. Installed skills appear in Evermind and can be keyword-triggered.')}
                            </div>
                            <div style={{ display: 'grid', gap: 8 }}>
                                <input
                                    value={sourceUrl}
                                    onChange={(e) => setSourceUrl(e.target.value)}
                                    placeholder="https://github.com/owner/repo/tree/main/path/to/skill"
                                    className="w-full bg-white/5 border border-white/10 rounded-md px-3 py-2 text-[11px] text-[var(--text1)]"
                                />
                                <input
                                    value={skillName}
                                    onChange={(e) => setSkillName(e.target.value)}
                                    placeholder={tr('可选：自定义技能名', 'Optional: custom skill name')}
                                    className="w-full bg-white/5 border border-white/10 rounded-md px-3 py-2 text-[11px] text-[var(--text1)]"
                                />
                                <input
                                    value={nodeTypesInput}
                                    onChange={(e) => setNodeTypesInput(e.target.value)}
                                    placeholder={tr('适用节点，例如 builder,tester', 'Node types, e.g. builder,tester')}
                                    className="w-full bg-white/5 border border-white/10 rounded-md px-3 py-2 text-[11px] text-[var(--text1)]"
                                />
                                <input
                                    value={keywordsInput}
                                    onChange={(e) => setKeywordsInput(e.target.value)}
                                    placeholder={tr('触发关键词，例如 video,trailer,分镜', 'Trigger keywords, e.g. video,trailer,storyboard')}
                                    className="w-full bg-white/5 border border-white/10 rounded-md px-3 py-2 text-[11px] text-[var(--text1)]"
                                />
                                <input
                                    value={tagsInput}
                                    onChange={(e) => setTagsInput(e.target.value)}
                                    placeholder={tr('标签，例如 video,motion', 'Tags, e.g. video,motion')}
                                    className="w-full bg-white/5 border border-white/10 rounded-md px-3 py-2 text-[11px] text-[var(--text1)]"
                                />
                                <button className="btn btn-primary text-[11px]" onClick={handleInstall} disabled={installing}>
                                    {installing ? tr('安装中...', 'Installing...') : tr('从 GitHub 安装', 'Install From GitHub')}
                                </button>
                                {statusText && (
                                    <div style={{
                                        fontSize: 11,
                                        color: statusText.includes('Installed') || statusText.includes('已安装') ? '#22c55e' : 'var(--text2)',
                                        background: 'rgba(255,255,255,0.04)',
                                        border: '1px solid var(--glass-border)',
                                        borderRadius: 10,
                                        padding: '8px 10px',
                                        wordBreak: 'break-word',
                                    }}>
                                        {statusText}
                                    </div>
                                )}
                            </div>
                        </section>
                    </div>

                    <div style={{ display: 'grid', gap: 14, alignContent: 'start' }}>
                        <section style={{ border: '1px solid var(--glass-border)', borderRadius: 14, padding: 14, background: 'rgba(255,255,255,0.03)' }}>
                            <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 10 }}>
                                <div style={{ fontSize: 13, fontWeight: 700 }}>{tr('任务能力卡', 'Task Capability Cards')}</div>
                                <div style={{ fontSize: 11, color: 'var(--text3)' }}>{tr('你没在 App 里看到的 dashboard / video / docs，都在这里明确展示。', 'This is where dashboard / video / docs capabilities are now surfaced in the app.')}</div>
                            </div>
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 10 }}>
                                {CAPABILITIES.map((item) => (
                                    <div key={item.id} style={{ border: '1px solid rgba(255,255,255,0.08)', borderRadius: 12, padding: 12, background: 'rgba(255,255,255,0.025)', display: 'grid', gap: 8 }}>
                                        <div style={{ fontSize: 12, fontWeight: 700 }}>{lang === 'zh' ? item.titleZh : item.titleEn}</div>
                                        <div style={{ fontSize: 11, color: 'var(--text2)', lineHeight: 1.5 }}>
                                            {lang === 'zh' ? item.summaryZh : item.summaryEn}
                                        </div>
                                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                                            {item.tags.map((tag) => (
                                                <span key={tag} style={{ fontSize: 10, padding: '3px 7px', borderRadius: 999, background: 'rgba(91,140,255,0.12)', color: 'var(--blue)' }}>
                                                    {tag}
                                                </span>
                                            ))}
                                        </div>
                                        <div style={{ fontSize: 10, color: 'var(--text3)', lineHeight: 1.45 }}>
                                            {item.example}
                                        </div>
                                        <button className="btn text-[10px]" onClick={() => void copyPrompt(item.example)}>
                                            {tr('复制示例需求', 'Copy Example Prompt')}
                                        </button>
                                    </div>
                                ))}
                            </div>
                        </section>

                        <section style={{ border: '1px solid var(--glass-border)', borderRadius: 14, padding: 14, background: 'rgba(255,255,255,0.03)' }}>
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 10 }}>
                                <div>
                                    <div style={{ fontSize: 13, fontWeight: 700 }}>{tr('已安装技能', 'Installed Skills')}</div>
                                    <div style={{ fontSize: 11, color: 'var(--text3)', marginTop: 4 }}>
                                        {tr(`当前 ${skills.length} 个技能，其中内置 ${skills.filter((s) => s.origin === 'builtin').length} 个，社区 ${skills.filter((s) => s.origin === 'community').length} 个。`, `Currently ${skills.length} skills: ${skills.filter((s) => s.origin === 'builtin').length} built-in, ${skills.filter((s) => s.origin === 'community').length} community.`)}
                                    </div>
                                </div>
                                <input
                                    value={search}
                                    onChange={(e) => setSearch(e.target.value)}
                                    placeholder={tr('搜索技能 / 标签 / 节点', 'Search skills / tags / node types')}
                                    className="bg-white/5 border border-white/10 rounded-md px-3 py-2 text-[11px] text-[var(--text1)]"
                                    style={{ minWidth: 240 }}
                                />
                            </div>

                            {loading ? (
                                <div style={{ fontSize: 12, color: 'var(--text3)' }}>{tr('正在加载技能库...', 'Loading skill library...')}</div>
                            ) : error ? (
                                <div style={{ fontSize: 12, color: '#ef4444' }}>{error}</div>
                            ) : (
                                <div style={{ display: 'grid', gap: 14 }}>
                                    <div>
                                        <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8 }}>{tr('内置技能', 'Built-in Skills')}</div>
                                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10 }}>
                                            {builtinSkills.map((skill) => (
                                                <SkillCard key={skill.name} skill={skill} lang={lang} />
                                            ))}
                                        </div>
                                    </div>

                                    <div>
                                        <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 8 }}>{tr('社区技能', 'Community Skills')}</div>
                                        {communitySkills.length === 0 ? (
                                            <div style={{ fontSize: 11, color: 'var(--text3)' }}>
                                                {tr('还没有社区技能。你可以在左侧通过 GitHub 链接安装。', 'No community skills yet. Install them from the GitHub form on the left.')}
                                            </div>
                                        ) : (
                                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10 }}>
                                                {communitySkills.map((skill) => (
                                                    <SkillCard key={skill.name} skill={skill} lang={lang} onDelete={() => void handleDelete(skill.name)} />
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            )}
                        </section>
                    </div>
                </div>
            </div>
        </div>
    );
}

function SkillCard({
    skill,
    lang,
    onDelete,
}: {
    skill: SkillLibraryRecord;
    lang: 'en' | 'zh';
    onDelete?: () => void;
}) {
    return (
        <div style={{ border: '1px solid rgba(255,255,255,0.08)', borderRadius: 12, padding: 12, background: 'rgba(255,255,255,0.025)', display: 'grid', gap: 8 }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 10 }}>
                <div>
                    <div style={{ fontSize: 12, fontWeight: 700 }}>{skill.title}</div>
                    <div style={{ fontSize: 10, color: 'var(--text3)', marginTop: 3 }}>
                        {skill.name} · {skill.origin === 'builtin' ? (lang === 'zh' ? '内置' : 'Built-in') : (lang === 'zh' ? '社区' : 'Community')}
                    </div>
                </div>
                {onDelete && (
                    <button className="btn text-[10px]" onClick={onDelete}>
                        {lang === 'zh' ? '移除' : 'Remove'}
                    </button>
                )}
            </div>

            <div style={{ fontSize: 11, color: 'var(--text2)', lineHeight: 1.5 }}>
                {skill.summary}
            </div>

            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {(skill.tags || []).map((tag) => (
                    <span key={tag} style={{ fontSize: 10, padding: '3px 7px', borderRadius: 999, background: 'rgba(34,197,94,0.10)', color: '#22c55e' }}>
                        {tag}
                    </span>
                ))}
                {(skill.node_types || []).map((nodeType) => (
                    <span key={nodeType} style={{ fontSize: 10, padding: '3px 7px', borderRadius: 999, background: 'rgba(168,85,247,0.12)', color: '#c084fc' }}>
                        {nodeType}
                    </span>
                ))}
            </div>

            <div style={{ fontSize: 10, color: 'var(--text3)', lineHeight: 1.45 }}>
                <strong>{lang === 'zh' ? '来源' : 'Source'}:</strong> {skill.source_name}
                {skill.license_note ? ` · ${skill.license_note}` : ''}
            </div>

            {skill.example_goal && (
                <div style={{ fontSize: 10, color: 'var(--text3)', lineHeight: 1.45 }}>
                    <strong>{lang === 'zh' ? '适用示例' : 'Example'}:</strong> {skill.example_goal}
                </div>
            )}

            {skill.source_url && (
                <a href={skill.source_url} target="_blank" rel="noreferrer" style={{ fontSize: 10, color: 'var(--blue)' }}>
                    {lang === 'zh' ? '查看来源' : 'Open Source Link'}
                </a>
            )}
        </div>
    );
}
