'use client';

import { useMemo, useState } from 'react';
import { type RunReportRecord } from '@/lib/types';

interface ReportsModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
    reports: RunReportRecord[];
    onDeleteReport: (id: string) => void;
    onClearReports: () => void;
}

const AGENT_NAME_ZH: Record<string, string> = {
    builder: '构建者', reviewer: '审查员', tester: '测试员',
    deployer: '部署者', analyst: '分析师', planner: '规划师',
    debugger: '调试员', scribe: '记录员', router: '路由器',
};

function agentLabel(agent: string, lang: 'en' | 'zh'): string {
    if (lang === 'zh') return AGENT_NAME_ZH[agent.toLowerCase()] || agent;
    return agent.charAt(0).toUpperCase() + agent.slice(1);
}

function statusLabel(status: string, lang: 'en' | 'zh'): string {
    if (lang !== 'zh') return status;
    const map: Record<string, string> = {
        completed: '已完成', failed: '失败',
        running: '进行中', retrying: '重试中', unknown: '未知',
    };
    return map[status.toLowerCase()] || status;
}

function formatDuration(seconds: number, lang: 'en' | 'zh'): string {
    const rounded = Math.round(seconds);
    if (rounded < 60) return lang === 'zh' ? `${rounded} 秒` : `${rounded}s`;
    const m = Math.floor(rounded / 60);
    const s = rounded % 60;
    return lang === 'zh' ? `${m} 分 ${s} 秒` : `${m}m ${s}s`;
}

function formatTs(ts: number, lang: 'en' | 'zh'): string {
    try {
        return new Date(ts).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', {
            month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', hour12: false,
        });
    } catch { return String(ts); }
}

function formatNodeTs(ts: number | undefined, lang: 'en' | 'zh'): string {
    if (!ts || !Number.isFinite(ts)) return '';
    try {
        return new Date(ts).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', {
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
        });
    } catch {
        return '';
    }
}

function parseJsonFromText(text: string): Record<string, unknown> | null {
    const raw = (text || '').trim();
    if (!raw) return null;
    try {
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
    } catch {
        // noop
    }
    const fenced = raw.match(/```json\s*([\s\S]*?)```/i);
    if (fenced?.[1]) {
        try {
            const parsed = JSON.parse(fenced[1].trim());
            return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
        } catch {
            // noop
        }
    }
    const start = raw.indexOf('{');
    const end = raw.lastIndexOf('}');
    if (start >= 0 && end > start) {
        const maybe = raw.slice(start, end + 1);
        try {
            const parsed = JSON.parse(maybe);
            return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
        } catch {
            return null;
        }
    }
    return null;
}

function shortText(input: string, max = 120): string {
    const clean = (input || '').replace(/\s+/g, ' ').trim();
    if (!clean) return '';
    return clean.length > max ? `${clean.slice(0, max)}...` : clean;
}

function extractPreviewUrl(text: string): string {
    const m = (text || '').match(/https?:\/\/127\.0\.0\.1:\d+\/preview\/[^\s"'`]+/i);
    return m?.[0] || '';
}

function collectIssuesFromJson(obj: Record<string, unknown> | null): string[] {
    if (!obj) return [];
    const out: string[] = [];
    const candidates = ['issues', 'errors', 'visual_issues', 'warnings', 'fixes'];
    for (const key of candidates) {
        const value = obj[key];
        if (Array.isArray(value)) {
            for (const item of value) {
                if (typeof item === 'string' && item.trim()) out.push(shortText(item, 90));
            }
        } else if (typeof value === 'string' && value.trim()) {
            out.push(shortText(value, 90));
        }
    }
    return out.slice(0, 4);
}

function humanNarrative(
    st: RunReportRecord['subtasks'][number],
    lang: 'en' | 'zh',
): { headline: string; actions: string[]; findings: string[]; conclusion: string; workSummary: string[] } {
    const agentType = st.agent.toLowerCase();
    const ok = st.status === 'completed';
    const taskText = shortText(st.task || '', 160);
    const outputText = st.outputPreview || '';
    const parsed = parseJsonFromText(outputText);
    const issues = collectIssuesFromJson(parsed);
    const previewUrl = extractPreviewUrl(`${st.task || ''}\n${outputText}`);
    const timeline = Array.isArray(st.timelineEvents) ? st.timelineEvents : [];
    const actions: string[] = [];
    const findings: string[] = [];
    // Work summary from backend analysis
    const workSummary: string[] = Array.isArray(st.workSummary) ? st.workSummary : [];

    if (taskText) {
        actions.push(lang === 'zh' ? `接收任务：${taskText}` : `Task received: ${taskText}`);
    }

    if (agentType === 'builder') {
        // Use work_summary for rich description
        if (workSummary.length) {
            for (const item of workSummary) {
                actions.push(item);
            }
        } else {
            actions.push(lang === 'zh' ? '执行代码/页面生成，并尝试写入产物文件' : 'Generated code/page and attempted artifact write');
        }
        if (/index\.html|written|files_created|save/i.test(outputText + taskText) && !workSummary.some(s => /文件/.test(s))) {
            actions.push(lang === 'zh' ? '已产出主文件（index.html）并准备预览' : 'Produced main file (index.html) and prepared preview');
        }
        if (/retry|重试/i.test(outputText) || st.retries > 0) {
            findings.push(lang === 'zh' ? `构建阶段出现重试（${st.retries} 次）` : `Build stage retried (${st.retries})`);
        }
    } else if (agentType === 'reviewer') {
        if (workSummary.length) {
            for (const item of workSummary) actions.push(item);
        } else {
            actions.push(lang === 'zh' ? '执行代码审查与可视化检查（浏览器/截图）' : 'Performed code review with visual checks (browser/screenshots)');
        }
        if (issues.length) findings.push(...issues.map((x) => lang === 'zh' ? `发现问题：${x}` : `Issue found: ${x}`));
        if (/approved|通过|good/i.test(outputText) && !workSummary.some(s => /审查/.test(s))) {
            findings.push(lang === 'zh' ? '审查结论：当前版本可继续流转' : 'Review verdict: safe to continue');
        }
    } else if (agentType === 'tester') {
        if (workSummary.length) {
            for (const item of workSummary) actions.push(item);
        } else {
            actions.push(lang === 'zh' ? '执行结构验证与页面可视化测试' : 'Ran structural + visual validation');
        }
        if (/visual_score/i.test(outputText) && !workSummary.some(s => /评分/.test(s))) {
            const m = outputText.match(/visual_score["'\s:]+([0-9]+)/i);
            if (m?.[1]) findings.push(lang === 'zh' ? `视觉评分：${m[1]}/10` : `Visual score: ${m[1]}/10`);
        }
        if (issues.length) findings.push(...issues.map((x) => lang === 'zh' ? `测试发现：${x}` : `Test finding: ${x}`));
    } else if (agentType === 'deployer') {
        actions.push(lang === 'zh' ? '检查产物目录并确认预览可访问路径' : 'Checked artifact directory and preview accessibility');
        if (previewUrl) findings.push(lang === 'zh' ? `预览链接：${previewUrl}` : `Preview URL: ${previewUrl}`);
    } else if (agentType === 'analyst') {
        if (workSummary.length) {
            for (const item of workSummary) actions.push(item);
        } else {
            actions.push(lang === 'zh' ? '调研参考案例并沉淀可执行建议' : 'Researched references and extracted actionable suggestions');
        }
    } else if (agentType === 'debugger') {
        actions.push(lang === 'zh' ? '根据错误日志定位根因并回修' : 'Located root cause from failures and applied fixes');
    } else if (agentType === 'planner') {
        actions.push(lang === 'zh' ? '拆分任务并定义执行依赖' : 'Split tasks and defined execution dependencies');
    } else {
        if (workSummary.length) {
            for (const item of workSummary) actions.push(item);
        } else {
            actions.push(lang === 'zh' ? '执行节点任务并回传结果' : 'Executed node task and returned results');
        }
    }

    // Files created
    const filesCreated = Array.isArray(st.filesCreated) ? st.filesCreated : [];
    if (filesCreated.length && !workSummary.some(s => /文件/.test(s))) {
        actions.push(lang === 'zh' ? `创建文件：${filesCreated.join(', ')}` : `Files created: ${filesCreated.join(', ')}`);
    }

    if (st.error) {
        findings.push(lang === 'zh' ? `错误：${shortText(st.error, 120)}` : `Error: ${shortText(st.error, 120)}`);
    }
    if (st.retries > 0) {
        findings.push(lang === 'zh' ? `该节点发生重试：${st.retries} 次` : `Node retried: ${st.retries}`);
    }
    if (typeof st.durationSeconds === 'number' && st.durationSeconds > 0) {
        actions.push(lang === 'zh' ? `节点耗时：约 ${st.durationSeconds} 秒` : `Duration: ~${st.durationSeconds}s`);
    }
    const startText = formatNodeTs(st.startedAt, lang);
    const endText = formatNodeTs(st.endedAt, lang);
    if (startText) {
        actions.push(lang === 'zh' ? `开始时间：${startText}` : `Started at: ${startText}`);
    }
    if (endText) {
        actions.push(lang === 'zh' ? `结束时间：${endText}` : `Ended at: ${endText}`);
    }
    if (timeline.length) {
        const sample = timeline.slice(-2);
        for (const item of sample) {
            findings.push(lang === 'zh' ? `过程记录：${shortText(item, 120)}` : `Execution note: ${shortText(item, 120)}`);
        }
    }
    if (!findings.length) {
        findings.push(lang === 'zh' ? '未返回额外细节，建议展开查看原始输出。' : 'No extra details returned, expand to inspect raw output.');
    }

    const headline = ok
        ? (lang === 'zh' ? '节点执行完成' : 'Node completed')
        : (lang === 'zh' ? '节点执行失败' : 'Node failed');
    const conclusion = ok
        ? (lang === 'zh' ? '结论：该节点结果可用，已进入下一步。' : 'Conclusion: output is usable and forwarded.')
        : (lang === 'zh' ? '结论：该节点未通过，需要修复后重跑。' : 'Conclusion: failed, requires fix and rerun.');

    return { headline, actions: actions.slice(0, 12), findings: findings.slice(0, 8), conclusion, workSummary };
}

/** Summarize what an agent did in plain language */
function summarizeAgent(st: RunReportRecord['subtasks'][number], lang: 'en' | 'zh'): string {
    const narrative = humanNarrative(st, lang);
    if (lang === 'zh') {
        const firstAction = narrative.actions[0] || '已执行任务';
        return `${narrative.headline}：${firstAction}`;
    }
    const firstAction = narrative.actions[0] || 'Task executed';
    return `${narrative.headline}: ${firstAction}`;
}

function reportMarkdown(report: RunReportRecord, lang: 'en' | 'zh'): string {
    const lines: string[] = [];
    if (lang === 'zh') {
        lines.push(`# Evermind 执行报告\n`);
        lines.push(`## 任务目标\n${report.goal}\n`);
        lines.push(`## 执行概览`);
        lines.push(`- 模式：${report.difficulty === 'simple' ? '极速' : report.difficulty === 'pro' ? '深度' : '平衡'}`);
        lines.push(`- 结果：${report.success ? '成功' : '失败'}`);
        lines.push(`- 完成：${report.completed}/${report.totalSubtasks} 个节点`);
        lines.push(`- 耗时：${formatDuration(report.durationSeconds, 'zh')}`);
        if (report.totalRetries > 0) lines.push(`- 重试：${report.totalRetries} 次`);
        if (report.previewUrl) lines.push(`- 预览：${report.previewUrl}`);
        lines.push(`\n## 执行详情\n`);
        for (const st of report.subtasks) {
            const narrative = humanNarrative(st, 'zh');
            lines.push(`### ${agentLabel(st.agent, 'zh')} #${st.id}`);
            lines.push(`- ${narrative.headline}`);
            for (const item of narrative.actions) lines.push(`- 动作：${item}`);
            for (const item of narrative.findings) lines.push(`- 发现：${item}`);
            if (Array.isArray(st.timelineEvents) && st.timelineEvents.length) {
                lines.push(`- 执行轨迹：`);
                for (const ev of st.timelineEvents.slice(-10)) lines.push(`  - ${ev}`);
            }
            lines.push(`- ${narrative.conclusion}`);
            if (st.retries > 0) lines.push(`- 重试 ${st.retries} 次后完成`);
            lines.push('');
        }
    } else {
        lines.push(`# Evermind Run Report\n`);
        lines.push(`## Goal\n${report.goal}\n`);
        lines.push(`## Summary`);
        lines.push(`- Mode: ${report.difficulty === 'simple' ? 'Blitz' : report.difficulty === 'pro' ? 'Deep' : 'Balanced'}`);
        lines.push(`- Result: ${report.success ? 'Success' : 'Failed'}`);
        lines.push(`- Completed: ${report.completed}/${report.totalSubtasks} nodes`);
        lines.push(`- Duration: ${formatDuration(report.durationSeconds, 'en')}`);
        if (report.totalRetries > 0) lines.push(`- Retries: ${report.totalRetries}`);
        if (report.previewUrl) lines.push(`- Preview: ${report.previewUrl}`);
        lines.push(`\n## Execution Details\n`);
        for (const st of report.subtasks) {
            const narrative = humanNarrative(st, 'en');
            lines.push(`### ${agentLabel(st.agent, 'en')} #${st.id}`);
            lines.push(`- ${narrative.headline}`);
            for (const item of narrative.actions) lines.push(`- Action: ${item}`);
            for (const item of narrative.findings) lines.push(`- Finding: ${item}`);
            if (Array.isArray(st.timelineEvents) && st.timelineEvents.length) {
                lines.push(`- Timeline:`);
                for (const ev of st.timelineEvents.slice(-10)) lines.push(`  - ${ev}`);
            }
            lines.push(`- ${narrative.conclusion}`);
            if (st.retries > 0) lines.push(`- Completed after ${st.retries} retries`);
            lines.push('');
        }
    }
    return lines.join('\n');
}

function downloadText(content: string, filename: string) {
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

const DIFFICULTY_LABEL: Record<string, Record<string, string>> = {
    simple:   { en: 'Blitz',    zh: '极速' },
    standard: { en: 'Balanced', zh: '平衡' },
    pro:      { en: 'Deep',     zh: '深度' },
};

export default function ReportsModal({
    open, onClose, lang, reports, onDeleteReport, onClearReports,
}: ReportsModalProps) {
    const [activeId, setActiveId] = useState<string>('');
    const [search, setSearch] = useState('');
    const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());

    const t = (en: string, zh: string) => (lang === 'zh' ? zh : en);

    const toggleNode = (key: string) => {
        setExpandedNodes(prev => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key); else next.add(key);
            return next;
        });
    };

    const filtered = useMemo(() => {
        if (!search.trim()) return reports;
        const q = search.toLowerCase();
        return reports.filter((r) => r.goal.toLowerCase().includes(q) || r.difficulty.toLowerCase().includes(q));
    }, [reports, search]);

    const active = useMemo(() => {
        if (!filtered.length) return null;
        if (!activeId) return filtered[0];
        return filtered.find((r) => r.id === activeId) || filtered[0];
    }, [filtered, activeId]);

    if (!open) return null;

    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal-container" onClick={(e) => e.stopPropagation()} style={{ width: 860, maxWidth: '92vw' }}>
                <div className="modal-header">
                    <h3>{t('Report Center', '报告中心')}</h3>
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button className="btn text-[11px]" onClick={onClearReports} disabled={!reports.length}>
                            {t('Clear All', '清空全部')}
                        </button>
                        <button className="modal-close" onClick={onClose}>✕</button>
                    </div>
                </div>

                <div style={{ padding: '8px 16px 0' }}>
                    <input
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder={t('Search reports...', '搜索报告...')}
                        style={{
                            width: '100%', padding: '6px 10px', fontSize: 11, borderRadius: 8,
                            border: '1px solid rgba(255,255,255,0.1)', background: 'rgba(255,255,255,0.04)',
                            color: 'var(--text1)', outline: 'none',
                        }}
                    />
                </div>

                <div className="modal-body" style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gap: 12, minHeight: 430 }}>
                    {/* Left: report list */}
                    <div className="glass" style={{ borderRadius: 10, padding: 8, overflow: 'auto', minWidth: 0 }}>
                        {filtered.length === 0 ? (
                            <div style={{ fontSize: 11, color: 'var(--text3)' }}>
                                {t('No reports yet', '暂无报告')}
                            </div>
                        ) : filtered.map((report) => {
                            const isActive = active?.id === report.id;
                            const modeLabel = DIFFICULTY_LABEL[report.difficulty]?.[lang] || report.difficulty;
                            return (
                                <button
                                    key={report.id}
                                    className="btn"
                                    onClick={() => setActiveId(report.id)}
                                    style={{
                                        width: '100%', display: 'block', textAlign: 'left',
                                        marginBottom: 6, padding: '7px 8px',
                                        borderColor: isActive ? 'var(--blue)' : 'var(--glass-border)',
                                        background: isActive ? 'rgba(79,143,255,0.08)' : undefined,
                                        overflow: 'hidden',
                                    }}
                                >
                                    <div style={{ fontSize: 9, color: report.success ? 'var(--green)' : 'var(--red)', marginBottom: 2 }}>
                                        {report.success ? '✓' : '✗'} {report.completed}/{report.totalSubtasks} · {modeLabel}
                                    </div>
                                    <div style={{
                                        fontSize: 10, color: 'var(--text1)', fontWeight: 700, marginBottom: 2,
                                        lineHeight: 1.35, overflow: 'hidden', textOverflow: 'ellipsis',
                                        display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' as const,
                                    }}>
                                        {report.goal.slice(0, 60)}
                                    </div>
                                    <div style={{ fontSize: 9, color: 'var(--text3)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                        {formatTs(report.createdAt, lang)} · {formatDuration(report.durationSeconds, lang)}
                                    </div>
                                </button>
                            );
                        })}
                    </div>

                    {/* Right: report detail */}
                    <div className="glass" style={{ borderRadius: 10, padding: 16, overflow: 'auto', minWidth: 0 }}>
                        {!active ? (
                            <div style={{ fontSize: 12, color: 'var(--text3)' }}>
                                {t('Select a report from the left.', '请在左侧选择一份报告。')}
                            </div>
                        ) : (
                            <>
                                {/* Header */}
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
                                    <div style={{ fontSize: 15, fontWeight: 700, flex: 1, color: 'var(--text1)', minWidth: 120 }}>
                                        {active.success ? '✓' : '✗'} {t('Execution Report', '执行报告')}
                                    </div>
                                    <button
                                        className="btn text-[10px]"
                                        onClick={() => {
                                            const md = reportMarkdown(active, lang);
                                            const file = `evermind_report_${new Date(active.createdAt).toISOString().replace(/[:.]/g, '-')}.md`;
                                            downloadText(md, file);
                                        }}
                                    >
                                        {t('Export', '导出')}
                                    </button>
                                    <button className="btn text-[10px]" onClick={() => onDeleteReport(active.id)}>
                                        {t('Delete', '删除')}
                                    </button>
                                </div>

                                {/* Goal */}
                                <div style={{
                                    fontSize: 12, color: 'var(--text1)', fontWeight: 600, marginBottom: 12,
                                    padding: '10px 14px', borderRadius: 10,
                                    background: 'rgba(108,92,231,0.06)', border: '1px solid rgba(108,92,231,0.15)',
                                    lineHeight: 1.55, wordBreak: 'break-word' as const,
                                }}>
                                    {t('Goal: ', '目标：')}{active.goal}
                                </div>

                                {/* Overview stats */}
                                <div style={{
                                    display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, marginBottom: 14,
                                }}>
                                    {[
                                        { label: t('Mode', '模式'), value: DIFFICULTY_LABEL[active.difficulty]?.[lang] || active.difficulty },
                                        { label: t('Result', '结果'), value: active.success ? t('OK', '成功') : t('Fail', '失败') },
                                        { label: t('Nodes', '节点'), value: `${active.completed}/${active.totalSubtasks}` },
                                        { label: t('Duration', '耗时'), value: formatDuration(active.durationSeconds, lang) },
                                    ].map((item) => (
                                        <div key={item.label} style={{
                                            textAlign: 'center', padding: '8px 4px', borderRadius: 8,
                                            background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)',
                                            overflow: 'hidden',
                                        }}>
                                            <div style={{ fontSize: 9, color: 'var(--text3)', marginBottom: 3 }}>{item.label}</div>
                                            <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text1)', whiteSpace: 'nowrap' }}>{item.value}</div>
                                        </div>
                                    ))}
                                </div>

                                {/* Preview button */}
                                {active.previewUrl && (
                                    <div style={{ marginBottom: 14 }}>
                                        <button
                                            className="btn btn-primary text-[11px]"
                                            onClick={() => window.open(active.previewUrl, '_blank', 'noopener,noreferrer')}
                                            style={{ padding: '8px 16px' }}
                                        >
                                            {t('Open Preview', '打开预览')}
                                        </button>
                                    </div>
                                )}

                                {/* Agent details — clickable to expand */}
                                <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text1)', marginBottom: 8 }}>
                                    {t('Execution Details', '执行详情')}
                                    <span style={{ fontSize: 10, fontWeight: 400, color: 'var(--text3)', marginLeft: 8 }}>
                                        {t('(click to expand)', '（点击展开详情）')}
                                    </span>
                                </div>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                    {active.subtasks.map((st) => {
                                        const isOk = st.status === 'completed';
                                        const nodeKey = `${active.id}:${st.id}`;
                                        const isExpanded = expandedNodes.has(nodeKey);
                                        const narrative = humanNarrative(st, lang);
                                        return (
                                            <div
                                                key={nodeKey}
                                                onClick={() => toggleNode(nodeKey)}
                                                style={{
                                                    padding: '10px 14px', borderRadius: 10, cursor: 'pointer',
                                                    background: isOk ? 'rgba(63,185,80,0.04)' : 'rgba(248,81,73,0.04)',
                                                    border: `1px solid ${isOk ? 'rgba(63,185,80,0.12)' : 'rgba(248,81,73,0.12)'}`,
                                                    transition: 'background 0.15s',
                                                }}
                                            >
                                                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                                                    <span style={{ fontSize: 10, transition: 'transform 0.15s', transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)', display: 'inline-block' }}>
                                                        ▶
                                                    </span>
                                                    <span style={{
                                                        fontSize: 10, fontWeight: 700, padding: '2px 8px',
                                                        borderRadius: 6, background: isOk ? 'rgba(63,185,80,0.12)' : 'rgba(248,81,73,0.12)',
                                                        color: isOk ? 'var(--green)' : 'var(--red)',
                                                    }}>
                                                        #{st.id}
                                                    </span>
                                                    <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text1)' }}>
                                                        {agentLabel(st.agent, lang)}
                                                    </span>
                                                    <span style={{ fontSize: 10, color: isOk ? 'var(--green)' : 'var(--orange)', marginLeft: 'auto' }}>
                                                        {statusLabel(st.status, lang)}
                                                    </span>
                                                </div>
                                                <div style={{ fontSize: 11, color: 'var(--text2)', lineHeight: 1.5, paddingLeft: 20 }}>
                                                    {summarizeAgent(st, lang)}
                                                    {narrative.workSummary.length > 0 && (
                                                        <div style={{ marginTop: 4, fontSize: 10, color: 'var(--text3)', lineHeight: 1.6 }}>
                                                            {narrative.workSummary.slice(0, 3).map((item, idx) => (
                                                                <div key={`ws-${idx}`}>• {item}</div>
                                                            ))}
                                                        </div>
                                                    )}
                                                </div>
                                                {st.retries > 0 && (
                                                    <div style={{ fontSize: 10, color: 'var(--text3)', marginTop: 3, paddingLeft: 20 }}>
                                                        {lang === 'zh' ? `重试 ${st.retries} 次` : `${st.retries} retries`}
                                                    </div>
                                                )}

                                                {/* Expanded detail */}
                                                {isExpanded && (
                                                    <div style={{
                                                        marginTop: 8, paddingTop: 8, paddingLeft: 20,
                                                        borderTop: '1px solid rgba(255,255,255,0.06)',
                                                    }}>
                                                        <div style={{ marginBottom: 8 }}>
                                                            <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text3)', marginBottom: 4 }}>
                                                                {t('Human-readable interpretation', '人话解读')}
                                                            </div>
                                                            <div style={{
                                                                fontSize: 11, color: 'var(--text2)', lineHeight: 1.6,
                                                                background: 'rgba(108,92,231,0.06)',
                                                                border: '1px solid rgba(108,92,231,0.16)',
                                                                padding: '8px 10px', borderRadius: 8,
                                                            }}>
                                                                <div style={{ fontWeight: 600, color: isOk ? 'var(--green)' : 'var(--red)', marginBottom: 4 }}>
                                                                    {narrative.headline}
                                                                </div>
                                                                {narrative.actions.map((item, idx) => (
                                                                    <div key={`a-${idx}`}>• {item}</div>
                                                                ))}
                                                                {narrative.findings.map((item, idx) => (
                                                                    <div key={`f-${idx}`}>• {item}</div>
                                                                ))}
                                                                <div style={{ marginTop: 4 }}>• {narrative.conclusion}</div>
                                                            </div>
                                                        </div>
                                                        <div style={{ marginBottom: 8 }}>
                                                            <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text3)', marginBottom: 4 }}>
                                                                {t('Execution Metrics', '执行指标')}
                                                            </div>
                                                            <div style={{
                                                                fontSize: 11, color: 'var(--text2)', lineHeight: 1.6,
                                                                background: 'rgba(255,255,255,0.02)',
                                                                border: '1px solid rgba(255,255,255,0.08)',
                                                                padding: '8px 10px', borderRadius: 8,
                                                            }}>
                                                                <div>• {t('Status', '状态')}：{statusLabel(st.status, lang)}</div>
                                                                <div>• {t('Retries', '重试次数')}：{st.retries || 0}</div>
                                                                {typeof st.durationSeconds === 'number' && st.durationSeconds > 0 && (
                                                                    <div>• {t('Duration', '耗时')}：{formatDuration(st.durationSeconds, lang)}</div>
                                                                )}
                                                                {st.startedAt && (
                                                                    <div>• {t('Started At', '开始时间')}：{formatNodeTs(st.startedAt, lang)}</div>
                                                                )}
                                                                {st.endedAt && (
                                                                    <div>• {t('Ended At', '结束时间')}：{formatNodeTs(st.endedAt, lang)}</div>
                                                                )}
                                                            </div>
                                                        </div>
                                                        {Array.isArray(st.timelineEvents) && st.timelineEvents.length > 0 && (
                                                            <div style={{ marginBottom: 8 }}>
                                                                <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text3)', marginBottom: 4 }}>
                                                                    {t('Execution Timeline', '执行轨迹')}
                                                                </div>
                                                                <div style={{
                                                                    fontSize: 11, color: 'var(--text2)', lineHeight: 1.6,
                                                                    background: 'rgba(6,182,212,0.06)',
                                                                    border: '1px solid rgba(6,182,212,0.16)',
                                                                    padding: '8px 10px', borderRadius: 8,
                                                                    maxHeight: 200, overflow: 'auto',
                                                                }}>
                                                                    {st.timelineEvents.slice(-14).map((item, idx) => (
                                                                        <div key={`timeline-${idx}`}>• {item}</div>
                                                                    ))}
                                                                </div>
                                                            </div>
                                                        )}
                                                        {st.task && (
                                                            <div style={{ marginBottom: 6 }}>
                                                                <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text3)', marginBottom: 2 }}>
                                                                    {t('Task Description', '任务描述')}
                                                                </div>
                                                                <div style={{
                                                                    fontSize: 11, color: 'var(--text2)', lineHeight: 1.5,
                                                                    wordBreak: 'break-word' as const,
                                                                    background: 'rgba(255,255,255,0.02)',
                                                                    padding: '6px 10px', borderRadius: 6,
                                                                    maxHeight: 120, overflow: 'auto',
                                                                }}>
                                                                    {st.task}
                                                                </div>
                                                            </div>
                                                        )}
                                                        {st.outputPreview && (
                                                            <div style={{ marginBottom: 6 }}>
                                                                <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text3)', marginBottom: 2 }}>
                                                                    {t('Output', '输出内容')}
                                                                </div>
                                                                <div style={{
                                                                    fontSize: 10, color: 'var(--text2)', lineHeight: 1.5,
                                                                    wordBreak: 'break-word' as const,
                                                                    background: 'rgba(255,255,255,0.02)',
                                                                    padding: '6px 10px', borderRadius: 6,
                                                                    maxHeight: 180, overflow: 'auto',
                                                                    fontFamily: 'monospace',
                                                                }}>
                                                                    {st.outputPreview}
                                                                </div>
                                                            </div>
                                                        )}
                                                        {st.error && (
                                                            <div>
                                                                <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--red)', marginBottom: 2 }}>
                                                                    {t('Error', '错误信息')}
                                                                </div>
                                                                <div style={{
                                                                    fontSize: 10, color: 'var(--red)', lineHeight: 1.5,
                                                                    wordBreak: 'break-word' as const,
                                                                    background: 'rgba(248,81,73,0.06)',
                                                                    padding: '6px 10px', borderRadius: 6,
                                                                    maxHeight: 120, overflow: 'auto',
                                                                }}>
                                                                    {st.error}
                                                                </div>
                                                            </div>
                                                        )}
                                                        {!st.task && !st.outputPreview && !st.error && (
                                                            <div style={{ fontSize: 10, color: 'var(--text3)', fontStyle: 'italic' }}>
                                                                {t('No detailed output available.', '暂无详细输出。')}
                                                            </div>
                                                        )}
                                                    </div>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>
                            </>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
