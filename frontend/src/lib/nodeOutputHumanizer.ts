import type { CanvasNodeStatus } from '@/lib/types';

export type HumanizeLang = 'en' | 'zh';
export type HumanizeTone = 'info' | 'ok' | 'error' | 'sys';

export interface HumanizeLogEntry {
    ts?: number;
    msg?: string;
    type?: string;
}

export interface NodeHumanizeInput {
    lang: HumanizeLang;
    nodeType: string;
    status: string;
    phase?: string;
    taskDescription?: string;
    outputSummary?: string;
    lastOutput?: string;
    loadedSkills?: string[];
    logs?: HumanizeLogEntry[];
    durationText?: string;
}

export interface StructuredOutputSection {
    title: string;
    text: string;
    type: HumanizeTone;
}

export interface ActivityDescriptor {
    title: string;
    text: string;
    type: HumanizeTone;
    category:
        | 'task'
        | 'skills'
        | 'phase'
        | 'progress'
        | 'files'
        | 'preview'
        | 'browser'
        | 'quality'
        | 'review'
        | 'testing'
        | 'handoff'
        | 'recovery'
        | 'execution'
        | 'output'
        | 'error'
        | 'system';
    lowSignal?: boolean;
}

type AnalystTagMeta = {
    tag: string;
    titleZh: string;
    titleEn: string;
    type: HumanizeTone;
};

const ANALYST_TAGS: AnalystTagMeta[] = [
    { tag: 'reference_sites', titleZh: '参考站点', titleEn: 'Reference Sites', type: 'sys' },
    { tag: 'design_direction', titleZh: '设计方向', titleEn: 'Design Direction', type: 'info' },
    { tag: 'non_negotiables', titleZh: '不可妥协要求', titleEn: 'Non-Negotiables', type: 'error' },
    { tag: 'deliverables_contract', titleZh: '交付物契约', titleEn: 'Deliverables Contract', type: 'sys' },
    { tag: 'risk_register', titleZh: '风险清单', titleEn: 'Risk Register', type: 'error' },
    { tag: 'builder_1_handoff', titleZh: '构建者 1 任务书', titleEn: 'Builder 1 Handoff', type: 'info' },
    { tag: 'builder_2_handoff', titleZh: '构建者 2 任务书', titleEn: 'Builder 2 Handoff', type: 'info' },
    { tag: 'reviewer_handoff', titleZh: '审查员任务书', titleEn: 'Reviewer Handoff', type: 'sys' },
    { tag: 'tester_handoff', titleZh: '测试员任务书', titleEn: 'Tester Handoff', type: 'sys' },
    { tag: 'debugger_handoff', titleZh: '调试员任务书', titleEn: 'Debugger Handoff', type: 'sys' },
];

const PHASE_LABELS: Record<string, { zh: string; en: string }> = {
    planning: { zh: '任务规划', en: 'planning' },
    research: { zh: '资料研究', en: 'research' },
    drafting: { zh: '页面起稿', en: 'drafting the page' },
    implementation: { zh: '功能实现', en: 'implementing features' },
    implementing: { zh: '功能实现', en: 'implementing features' },
    coding: { zh: '编码中', en: 'coding' },
    assemble: { zh: '产物组装', en: 'assembling artifacts' },
    assembling: { zh: '产物组装', en: 'assembling artifacts' },
    handoff: { zh: '交付整理', en: 'preparing handoff' },
    reviewing: { zh: '质量审查', en: 'reviewing quality' },
    review: { zh: '质量审查', en: 'reviewing quality' },
    testing: { zh: '测试验证', en: 'running tests' },
    verification: { zh: '结果验证', en: 'verifying output' },
    preview: { zh: '预览整理', en: 'preparing preview' },
    preview_validation: { zh: '预览校验', en: 'validating preview' },
    waiting_ai: { zh: '等待模型响应', en: 'waiting for the model' },
    waiting_model: { zh: '等待模型响应', en: 'waiting for the model' },
    waiting_parts: { zh: '等待并行节点汇合', en: 'waiting for parallel nodes' },
    browser_testing: { zh: '浏览器验收', en: 'browser validation' },
    browser_validation: { zh: '浏览器验收', en: 'browser validation' },
    finalizing: { zh: '最终收口', en: 'finalizing' },
    debugging: { zh: '问题修复', en: 'debugging' },
    repair: { zh: '返工修复', en: 'repairing issues' },
    rework: { zh: '返工修复', en: 'repairing issues' },
};

function tr(lang: HumanizeLang, zh: string, en: string): string {
    return lang === 'zh' ? zh : en;
}

function clip(text: string, max: number): string {
    const normalized = text.trim();
    if (normalized.length <= max) return normalized;
    return `${normalized.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

function cleanInline(text: string): string {
    return String(text || '')
        .replace(/```[\w-]*\n?/g, ' ')
        .replace(/```/g, ' ')
        .replace(/\r/g, '\n')
        .replace(/\s+/g, ' ')
        .trim();
}

function stripMarkdownFence(text: string): string {
    const trimmed = String(text || '').trim();
    if (!trimmed.startsWith('```')) return trimmed;
    return trimmed
        .replace(/^```[\w-]*\n?/, '')
        .replace(/\n?```$/, '')
        .trim();
}

function cleanBlock(text: string): string {
    return stripMarkdownFence(text)
        .replace(/\r/g, '\n')
        .replace(/\u0000/g, '')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}

function dedupeList(items: string[]): string[] {
    const seen = new Set<string>();
    const result: string[] = [];
    for (const item of items) {
        const normalized = cleanInline(item);
        if (!normalized) continue;
        const key = normalized.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        result.push(normalized);
    }
    return result;
}

function splitStructuredLines(text: string): string[] {
    const normalized = cleanBlock(text)
        .replace(/<\/?[^>]+>/g, '\n');

    return dedupeList(
        normalized
            .split(/\n+/)
            .map((line) => line.replace(/^(?:[-*•]|\d+[.)])\s*/, '').trim())
            .filter(Boolean),
    );
}

function formatStructuredText(text: string, lang: HumanizeLang, maxLines = 4, maxChars = 360): string {
    const lines = splitStructuredLines(text);
    if (lines.length === 0) return '';
    if (lines.length === 1) return clip(lines[0], maxChars);

    const rows = lines.slice(0, maxLines).map((line, index) => `${index + 1}. ${clip(line, 150)}`);
    if (lines.length > maxLines) {
        rows.push(tr(lang, `还有 ${lines.length - maxLines} 条补充内容。`, `${lines.length - maxLines} more items.`));
    }
    return rows.join('\n');
}

function formatList(items: string[], lang: HumanizeLang, fallbackZh: string, fallbackEn: string): string {
    const normalized = dedupeList(items);
    if (normalized.length === 0) return tr(lang, fallbackZh, fallbackEn);
    return normalized
        .slice(0, 4)
        .map((item, index) => `${index + 1}. ${clip(item, 150)}`)
        .join('\n');
}

function formatKinds(kinds: string[], lang: HumanizeLang): string {
    if (kinds.length === 0) return tr(lang, '页面与交互', 'page and interaction');
    return kinds.join(lang === 'zh' ? ' / ' : ' / ');
}

function extractTagBlock(text: string, tag: string): string {
    const regex = new RegExp(`<${tag}>([\\s\\S]*?)<\\/${tag}>`, 'i');
    const match = cleanBlock(text).match(regex);
    return match ? match[1].trim() : '';
}

function extractJsonCandidate(text: string): string {
    const stripped = stripMarkdownFence(text);
    if (/^\s*[\[{]/.test(stripped)) return stripped.trim();
    const start = stripped.indexOf('{');
    const end = stripped.lastIndexOf('}');
    if (start >= 0 && end > start) return stripped.slice(start, end + 1).trim();
    return stripped.trim();
}

function coerceStringList(value: unknown): string[] {
    if (Array.isArray(value)) return dedupeList(value.map((item) => cleanInline(String(item || ''))));
    if (typeof value === 'string') {
        return dedupeList(
            value
                .split(/\n|;|；/)
                .map((item) => cleanInline(item))
                .filter(Boolean),
        );
    }
    return [];
}

function inferArtifactKinds(text: string): string[] {
    const normalized = cleanBlock(text);
    const lower = normalized.toLowerCase();
    const kinds: string[] = [];

    if (/(<!doctype html>|<html|<\/body>|<\/div>|<section|<main)/i.test(normalized)) kinds.push('HTML');
    if (/(@media|display\s*:\s*flex|font-family|background:|grid-template|padding:|margin:)/i.test(normalized)) kinds.push('CSS');
    if (/\b(function|const|let|var|=>|addEventListener|document\.|window\.|import\s.+from|export\s)/i.test(normalized)) kinds.push('JavaScript');
    if (/\b(def\s+\w+|from\s+\w+\s+import|print\(|pytest|async def)\b/i.test(normalized)) kinds.push('Python');
    if (/\b(interface\s+\w+|type\s+\w+\s*=|tsx?|useState\(|useEffect\()/i.test(normalized)) kinds.push('TypeScript');
    if (/<svg|viewBox=|stroke=|fill=/i.test(normalized)) kinds.push('SVG');
    if (/\bcanvas\b/i.test(lower) && /(\bctx\b|\bgetcontext\b)/i.test(lower)) kinds.push('Canvas');

    return dedupeList(kinds);
}

export function formatPhaseLabel(phase: string, lang: HumanizeLang): string {
    const normalized = cleanInline(phase).toLowerCase().replace(/[\s-]+/g, '_');
    if (!normalized) return '';
    const mapped = PHASE_LABELS[normalized];
    if (mapped) return lang === 'zh' ? mapped.zh : mapped.en;
    const readable = normalized.replace(/_/g, ' ').trim();
    return readable;
}

export function formatSkillLabel(skill: string): string {
    return cleanInline(skill)
        .replace(/[-_]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

export function looksLikeAnalystHandoff(text: string): boolean {
    const normalized = cleanBlock(text).toLowerCase();
    if (!normalized) return false;
    const tagMatches = ANALYST_TAGS.filter(({ tag }) => normalized.includes(`<${tag}>`) && normalized.includes(`</${tag}>`)).length;
    return tagMatches >= 2;
}

function parseReviewerPayload(text: string): {
    verdict: string;
    blockingIssues: string[];
    missingDeliverables: string[];
    requiredChanges: string[];
    acceptanceCriteria: string[];
    shipReadiness: string;
} | null {
    const candidate = extractJsonCandidate(text);
    if (!candidate || !candidate.startsWith('{')) return null;
    try {
        const parsed = JSON.parse(candidate) as Record<string, unknown>;
        const blockingIssues = coerceStringList(parsed.blocking_issues);
        const missingDeliverables = coerceStringList(parsed.missing_deliverables);
        const requiredChanges = coerceStringList(parsed.required_changes);
        const acceptanceCriteria = coerceStringList(parsed.acceptance_criteria);
        const shipReadiness = cleanInline(String(parsed.ship_readiness || ''));
        const verdict = cleanInline(String(parsed.verdict || parsed.decision || ''));
        if (!verdict && blockingIssues.length === 0 && missingDeliverables.length === 0 && requiredChanges.length === 0 && acceptanceCriteria.length === 0) {
            return null;
        }
        return { verdict, blockingIssues, missingDeliverables, requiredChanges, acceptanceCriteria, shipReadiness };
    } catch {
        return null;
    }
}

export function looksLikeReviewerPayload(text: string): boolean {
    return parseReviewerPayload(text) !== null;
}

function looksLikeGenericStructuredData(text: string): boolean {
    const normalized = cleanBlock(text);
    if (looksLikeAnalystHandoff(normalized) || looksLikeReviewerPayload(normalized)) return false;
    if (normalized.length < 24) return false;
    const jsonLike = (/^\s*\{[\s\S]*\}\s*$/.test(normalized) || /^\s*\[[\s\S]*\]\s*$/.test(normalized))
        && /["'][^"']+["']\s*:/.test(normalized);
    const xmlLike = /^<[\w:-]+>[\s\S]*<\/[\w:-]+>$/.test(normalized);
    return jsonLike || xmlLike;
}

export function looksLikeCode(text: string): boolean {
    const normalized = cleanBlock(text);
    if (!normalized || normalized.length < 40) return false;
    if (looksLikeAnalystHandoff(normalized) || looksLikeReviewerPayload(normalized)) return false;

    let score = 0;
    const patternMatches = [
        /<!doctype html>/i,
        /<script/i,
        /<style/i,
        /<\/[a-z]+>/i,
        /\bfunction\s+\w+/i,
        /\bconst\s+\w+/i,
        /\blet\s+\w+/i,
        /\bimport\s.+from\s/i,
        /\bexport\s/i,
        /=>/,
        /@media/i,
        /\breturn\b/i,
        /\buseState\(/,
    ];
    patternMatches.forEach((pattern) => {
        if (pattern.test(normalized)) score += 1;
    });

    const lines = normalized.split('\n');
    const codeLikeLines = lines.filter((line) => /[{};<>]/.test(line) && line.trim().length > 14).length;
    if (codeLikeLines >= 3) score += 2;

    const markupTokens = (normalized.match(/<\/?[a-z][^>]*>/gi) || []).length;
    if (markupTokens >= 6) score += 2;

    const braceAndSemicolonCount = (normalized.match(/[{};]/g) || []).length;
    if (braceAndSemicolonCount >= 10) score += 1;

    return score >= 3;
}

export function looksLikeMachineOutput(text: string): boolean {
    return looksLikeAnalystHandoff(text)
        || looksLikeReviewerPayload(text)
        || looksLikeCode(text)
        || looksLikeGenericStructuredData(text);
}

function summarizeAnalystHandoff(text: string, lang: HumanizeLang): string {
    const sections = ANALYST_TAGS.map((meta) => ({
        tag: meta.tag,
        value: extractTagBlock(text, meta.tag),
    })).filter((section) => section.value);

    const sectionTags = new Set(sections.map((section) => section.tag));
    const builderCount = ['builder_1_handoff', 'builder_2_handoff'].filter((tag) => sectionTags.has(tag)).length;
    const mentions: string[] = [];
    if (sectionTags.has('reference_sites')) mentions.push(tr(lang, '参考站点研究', 'reference-site research'));
    if (sectionTags.has('design_direction')) mentions.push(tr(lang, '设计方向', 'design direction'));
    if (sectionTags.has('non_negotiables')) mentions.push(tr(lang, '不可妥协要求', 'non-negotiables'));
    if (sectionTags.has('deliverables_contract')) mentions.push(tr(lang, '交付物契约', 'deliverables contract'));
    if (sectionTags.has('risk_register')) mentions.push(tr(lang, '风险清单', 'risk register'));
    if (builderCount > 0) mentions.push(tr(lang, `${builderCount} 份构建任务书`, `${builderCount} builder handoff${builderCount > 1 ? 's' : ''}`));
    if (sectionTags.has('reviewer_handoff') || sectionTags.has('tester_handoff') || sectionTags.has('debugger_handoff')) {
        mentions.push(tr(lang, '下游审查与测试交接说明', 'review and test handoffs'));
    }

    if (mentions.length === 0) {
        return tr(
            lang,
            '分析师已完成参考研究与下游任务拆解，原始 XML 已隐藏并改为可读摘要。',
            'The analyst finished research and downstream task decomposition; raw XML is hidden behind a readable summary.',
        );
    }

    return tr(
        lang,
        `分析师已完成研究与拆解，已整理 ${mentions.join('、')}。`,
        `The analyst finished the research brief and organized ${mentions.join(', ')}.`,
    );
}

function summarizeReviewerPayload(text: string, lang: HumanizeLang): string {
    const parsed = parseReviewerPayload(text);
    if (!parsed) {
        return tr(
            lang,
            '审查员已完成质量评审，原始 JSON 已隐藏并改为可读摘要。',
            'The reviewer finished the quality review; raw JSON is hidden behind a readable summary.',
        );
    }

    const verdict = parsed.verdict.toLowerCase();
    if (['approve', 'approved', 'pass', 'passed'].includes(verdict)) {
        return tr(
            lang,
            `审查员已完成质量评审，结论为通过，交付就绪度 ${parsed.shipReadiness || '已达标'}，并补充了 ${Math.max(parsed.acceptanceCriteria.length, 1)} 条验收标准。`,
            `The reviewer approved the output with ship readiness ${parsed.shipReadiness || 'at target'} and documented ${Math.max(parsed.acceptanceCriteria.length, 1)} acceptance criteria.`,
        );
    }

    if (['reject', 'rejected', 'needs_fix', 'blocked'].includes(verdict) || parsed.blockingIssues.length > 0 || parsed.requiredChanges.length > 0 || parsed.missingDeliverables.length > 0) {
        return tr(
            lang,
            `审查员已完成质量评审，发现 ${Math.max(parsed.blockingIssues.length, 1)} 个阻塞问题、${Math.max(parsed.missingDeliverables.length, 0)} 个缺失交付物，并给出 ${Math.max(parsed.requiredChanges.length, 1)} 条整改要求。`,
            `The reviewer found ${Math.max(parsed.blockingIssues.length, 1)} blocking issue${Math.max(parsed.blockingIssues.length, 1) > 1 ? 's' : ''}, ${Math.max(parsed.missingDeliverables.length, 0)} missing deliverable${Math.max(parsed.missingDeliverables.length, 0) === 1 ? '' : 's'}, and issued ${Math.max(parsed.requiredChanges.length, 1)} required change${Math.max(parsed.requiredChanges.length, 1) > 1 ? 's' : ''}.`,
        );
    }

    return tr(
        lang,
        `审查员已输出结构化评审结论，并沉淀了 ${Math.max(parsed.acceptanceCriteria.length, 1)} 条验收标准。`,
        `The reviewer produced a structured verdict and documented ${Math.max(parsed.acceptanceCriteria.length, 1)} acceptance criteria.`,
    );
}

function summarizeArtifactOutput(text: string, nodeType: string, status: string, lang: HumanizeLang): string {
    const kinds = inferArtifactKinds(text);
    const kindsText = formatKinds(kinds, lang);
    const normalizedStatus = String(status || '').trim().toLowerCase();

    if (['failed', 'error', 'blocked'].includes(normalizedStatus)) {
        return tr(
            lang,
            `该节点在处理 ${kindsText} 产物时遇到阻塞，界面已隐藏原始源码，避免直接展示难以阅读的片段。`,
            `This node hit a blocking issue while working on ${kindsText}; the raw source is hidden to avoid showing unreadable snippets directly.`,
        );
    }

    if (['passed', 'done', 'skipped'].includes(normalizedStatus)) {
        return tr(
            lang,
            `该节点已完成 ${kindsText} 产物输出，界面已隐藏原始源码；请结合文件写出、预览地址和质量校验记录查看结果。`,
            `This node finished generating ${kindsText}; the raw source is hidden, so use file output, preview links, and quality checks to inspect the result.`,
        );
    }

    if (String(nodeType || '').trim().toLowerCase() === 'builder') {
        return tr(
            lang,
            `构建者正在产出 ${kindsText}，界面已隐藏原始源码片段，当前只展示可读进度与关键动作。`,
            `The builder is producing ${kindsText}; raw source snippets are hidden so the UI can focus on readable progress and key actions.`,
        );
    }

    return tr(
        lang,
        `该节点正在处理 ${kindsText} 相关产物，原始结构化内容已隐藏并改为可读摘要。`,
        `This node is working on ${kindsText}-related output; the raw structured content is hidden behind a readable summary.`,
    );
}

function baseNodeSentence(nodeType: string, status: string, lang: HumanizeLang, durationText: string): string {
    const labels: Record<string, { runningZh: string; runningEn: string; doneZh: string; doneEn: string; failedZh: string; failedEn: string }> = {
        builder: {
            runningZh: '构建者正在实现页面结构、样式和交互。',
            runningEn: 'The builder is implementing structure, styling, and interaction.',
            doneZh: '构建者已完成页面与代码产出。',
            doneEn: 'The builder finished the page and code output.',
            failedZh: '构建者在实现过程中遇到阻塞，正在等待修复或返工。',
            failedEn: 'The builder hit a blocking issue and is waiting for repair or rework.',
        },
        reviewer: {
            runningZh: '审查员正在执行质量审查与验收判断。',
            runningEn: 'The reviewer is running the quality review and acceptance check.',
            doneZh: '审查员已完成质量审查。',
            doneEn: 'The reviewer finished the quality review.',
            failedZh: '审查环节发现阻塞问题，正在准备整改说明。',
            failedEn: 'The review found blocking issues and is preparing remediation details.',
        },
        tester: {
            runningZh: '测试员正在进行预览验收与规则校验。',
            runningEn: 'The tester is validating the preview and running rule checks.',
            doneZh: '测试员已完成验证与测试。',
            doneEn: 'The tester finished verification and testing.',
            failedZh: '测试环节发现问题，正在准备回传修复要求。',
            failedEn: 'Testing found issues and is preparing repair feedback.',
        },
        deployer: {
            runningZh: '部署员正在整理产物并准备可访问预览。',
            runningEn: 'The deployer is packaging artifacts and preparing a reachable preview.',
            doneZh: '部署员已完成产物整理与预览准备。',
            doneEn: 'The deployer finished artifact packaging and preview preparation.',
            failedZh: '部署或预览整理过程中出现阻塞。',
            failedEn: 'Deployment or preview preparation encountered a blocking issue.',
        },
        planner: {
            runningZh: '规划师正在梳理总体方案与节点分工。',
            runningEn: 'The planner is shaping the overall plan and node responsibilities.',
            doneZh: '规划师已完成总体方案整理。',
            doneEn: 'The planner finished the execution plan.',
            failedZh: '规划阶段存在阻塞，方案仍需补齐。',
            failedEn: 'Planning is blocked and the execution plan still needs refinement.',
        },
        analyst: {
            runningZh: '分析师正在研究参考资料并拆解下游任务书。',
            runningEn: 'The analyst is researching references and drafting downstream handoffs.',
            doneZh: '分析师已完成研究与任务拆解。',
            doneEn: 'The analyst finished the research and downstream decomposition.',
            failedZh: '分析师交付存在缺口，正在补齐研究或任务书。',
            failedEn: 'The analyst handoff is incomplete and needs more research or clearer briefs.',
        },
        debugger: {
            runningZh: '调试员正在定位问题并收敛风险。',
            runningEn: 'The debugger is tracing issues and reducing risk.',
            doneZh: '调试员已完成本轮问题修复。',
            doneEn: 'The debugger finished the current repair pass.',
            failedZh: '调试环节仍有未解决问题。',
            failedEn: 'The debugging pass still has unresolved issues.',
        },
    };

    const label = labels[nodeType] || labels.builder;
    const normalizedStatus = String(status || '').trim().toLowerCase() as CanvasNodeStatus;

    if (normalizedStatus === 'running') {
        return tr(lang, `${label.runningZh}${durationText ? ` 已持续 ${durationText}。` : ''}`, `${label.runningEn}${durationText ? ` ${durationText} elapsed.` : ''}`);
    }
    if (normalizedStatus === 'passed' || normalizedStatus === 'done' || normalizedStatus === 'skipped') {
        return tr(lang, `${label.doneZh}${durationText ? ` 总耗时 ${durationText}。` : ''}`, `${label.doneEn}${durationText ? ` Total time: ${durationText}.` : ''}`);
    }
    if (normalizedStatus === 'failed' || normalizedStatus === 'error' || normalizedStatus === 'blocked') {
        return tr(lang, label.failedZh, label.failedEn);
    }
    return tr(lang, label.runningZh, label.runningEn);
}

function getLatestHumanLog(logs: HumanizeLogEntry[] = [], lang: HumanizeLang): string {
    const ordered = [...logs].sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0));
    let fallback = '';
    for (let index = ordered.length - 1; index >= 0; index -= 1) {
        const text = humanizeLogMessage(String(ordered[index]?.msg || ''), lang);
        if (!text) continue;
        if (!fallback) fallback = text;
        if (!isLowSignalSummary(text)) return text;
    }
    return fallback;
}

function isLowSignalSummary(text: string): boolean {
    const normalized = cleanInline(text).toLowerCase();
    return [
        '原始代码片段已隐藏',
        '原始结构化内容已隐藏',
        '原始内容，避免直接展示',
        'readable summary',
        'raw source snippets are hidden',
        'raw structured content is hidden',
        'raw content is hidden',
        'expanded into readable entries',
    ].some((fragment) => normalized.includes(fragment.toLowerCase()));
}

function makeActivity(
    lang: HumanizeLang,
    category: ActivityDescriptor['category'],
    text: string,
    options?: {
        titleZh?: string;
        titleEn?: string;
        type?: HumanizeTone;
        lowSignal?: boolean;
    },
): ActivityDescriptor {
    return {
        title: lang === 'zh' ? (options?.titleZh || '执行记录') : (options?.titleEn || 'Execution Record'),
        text,
        type: options?.type || 'info',
        category,
        ...(options?.lowSignal ? { lowSignal: true } : {}),
    };
}

export function describeNodeActivity(
    message: string,
    lang: HumanizeLang,
    options?: { nodeType?: string; status?: string },
): ActivityDescriptor | null {
    const normalized = cleanBlock(message);
    if (!normalized) return null;

    if (/^(?:status update|状态更新)[:：]/i.test(normalized)) return null;

    if (
        /等待模型响应中|仍在执行中，请稍候|is still running, please wait|still running, please wait|waiting for the model/i.test(normalized)
        || /正在构建页面结构和核心组件|正在搜索相关参考案例和设计灵感|正在分析参考网站的配色|正在整理设计要点|规划较为复杂，仍在处理中/i.test(normalized)
    ) {
        return makeActivity(
            lang,
            'progress',
            tr(lang, '节点仍在等待模型返回结果，系统已继续保持执行。', 'The node is still waiting for the model response and remains in progress.'),
            { titleZh: '运行心跳', titleEn: 'Execution Heartbeat', lowSignal: true },
        );
    }

    const phaseMatch = normalized.match(/^(?:phase|阶段)[:：]\s*(.+)$/i);
    if (phaseMatch) {
        return makeActivity(
            lang,
            'phase',
            tr(
                lang,
                `阶段切换为：${formatPhaseLabel(phaseMatch[1], lang) || cleanInline(phaseMatch[1])}`,
                `Phase changed to: ${formatPhaseLabel(phaseMatch[1], lang) || cleanInline(phaseMatch[1])}`,
            ),
            { titleZh: '阶段变化', titleEn: 'Phase Update' },
        );
    }

    const toolMatch = normalized.match(/^(?:tool call|工具调用)[:：]\s*(.+)$/i);
    if (toolMatch) {
        return makeActivity(
            lang,
            'execution',
            tr(
                lang,
                `已调用工具：${clip(cleanInline(toolMatch[1]), 180)}`,
                `Tool invoked: ${clip(cleanInline(toolMatch[1]), 180)}`,
            ),
            { titleZh: '工具调用', titleEn: 'Tool Activity', type: 'sys' },
        );
    }

    if (/^(?:model output update|模型输出更新)/i.test(normalized)) {
        return makeActivity(
            lang,
            'progress',
            tr(
                lang,
                '模型已回传新的工作内容，界面正在整理成可读的进度摘要。',
                'The model returned new work and the UI is condensing it into a readable progress summary.',
            ),
            { titleZh: '模型进度', titleEn: 'Model Progress', lowSignal: true },
        );
    }

    if (/^(?:已加载技能|loaded skills)[:：]/i.test(normalized)) {
        return makeActivity(
            lang,
            'skills',
            normalized,
            { titleZh: '技能加载', titleEn: 'Skills Loaded', type: 'sys' },
        );
    }

    if (/^(?:任务说明|task brief|task)[:：]/i.test(normalized)) {
        return makeActivity(
            lang,
            'task',
            normalized,
            { titleZh: '任务说明', titleEn: 'Task Brief', type: 'sys' },
        );
    }

    if (/^(?:已注入仓库地图|repo map injected|repository map injected|仓库地图)[:：]?/i.test(normalized)) {
        return makeActivity(
            lang,
            'execution',
            normalized,
            { titleZh: '仓库地图', titleEn: 'Repo Map', type: 'sys' },
        );
    }

    if (/^(?:开始执行|execution started)[:：]/i.test(normalized)) {
        return makeActivity(
            lang,
            'execution',
            normalized,
            { titleZh: '开始执行', titleEn: 'Execution Started' },
        );
    }

    if (/^(?:执行完成|execution complete)[:：]/i.test(normalized)) {
        return makeActivity(
            lang,
            'execution',
            normalized,
            { titleZh: '执行完成', titleEn: 'Execution Completed', type: 'ok' },
        );
    }

    if (/^(?:执行失败|失败原因|execution failed|failure reason)[:：]/i.test(normalized)) {
        return makeActivity(
            lang,
            'error',
            normalized,
            { titleZh: '失败信息', titleEn: 'Failure Details', type: 'error' },
        );
    }

    if (/^(?:触发重试|retry triggered)[:：]/i.test(normalized)) {
        return makeActivity(
            lang,
            'recovery',
            normalized,
            { titleZh: '自动重试', titleEn: 'Automatic Retry', type: 'error' },
        );
    }

    if (/^(?:模型降级重试|model downgrade retry)[:：]/i.test(normalized)) {
        return makeActivity(
            lang,
            'recovery',
            normalized,
            { titleZh: '自动恢复', titleEn: 'Auto Recovery', type: 'sys' },
        );
    }

    if (
        /^(?:已写出 \d+ 个文件到|wrote \d+ file\(s\) to|生成文件[:：]|generated files[:：])/i.test(normalized)
    ) {
        return makeActivity(
            lang,
            'files',
            normalized,
            { titleZh: '文件产出', titleEn: 'Files Written', type: 'ok' },
        );
    }

    if (/^(?:预览地址已生成|预览已就绪|preview ready|preview url ready)[:：]?/i.test(normalized)) {
        return makeActivity(
            lang,
            'preview',
            normalized,
            { titleZh: '预览准备', titleEn: 'Preview Ready', type: 'ok' },
        );
    }

    if (/^(?:浏览器步骤|browser step)[:：]/i.test(normalized) || /entered browser testing/i.test(normalized)) {
        return makeActivity(
            lang,
            'browser',
            normalized,
            { titleZh: '浏览器验证', titleEn: 'Browser Validation' },
        );
    }

    if (/^(?:质量门|quality gate)/i.test(normalized)) {
        return makeActivity(
            lang,
            'quality',
            normalized,
            {
                titleZh: '质量门',
                titleEn: 'Quality Gate',
                type: /失败|failed/i.test(normalized) ? 'error' : 'info',
            },
        );
    }

    if (/审查员打回作品|reviewer rejected|审查结论|review verdict/i.test(normalized)) {
        return makeActivity(
            lang,
            'review',
            normalized,
            {
                titleZh: '审查结果',
                titleEn: 'Review Outcome',
                type: /打回|reject|blocked|needs fix/i.test(normalized) ? 'error' : 'ok',
            },
        );
    }

    if (/测试员确定性验收|tester deterministic gate|规则校验/i.test(normalized)) {
        return makeActivity(
            lang,
            'testing',
            normalized,
            {
                titleZh: '测试结果',
                titleEn: 'Test Result',
                type: /失败|failed/i.test(normalized) ? 'error' : 'ok',
            },
        );
    }

    if (/分析师交付不完整|analyst handoff incomplete/i.test(normalized)) {
        return makeActivity(
            lang,
            'handoff',
            normalized,
            { titleZh: '分析交付', titleEn: 'Analyst Handoff', type: 'error' },
        );
    }

    if (looksLikeAnalystHandoff(normalized)) {
        return makeActivity(
            lang,
            'handoff',
            summarizeAnalystHandoff(normalized, lang),
            { titleZh: '研究交付', titleEn: 'Research Handoff', type: 'sys' },
        );
    }

    if (looksLikeReviewerPayload(normalized)) {
        return makeActivity(
            lang,
            'review',
            summarizeReviewerPayload(normalized, lang),
            {
                titleZh: '审查结果',
                titleEn: 'Review Outcome',
                type: /reject|needs_fix|blocked/i.test(normalized) ? 'error' : 'ok',
            },
        );
    }

    if (looksLikeCode(normalized) || looksLikeGenericStructuredData(normalized)) {
        return makeActivity(
            lang,
            'output',
            summarizeArtifactOutput(normalized, options?.nodeType || 'builder', options?.status || '', lang),
            { titleZh: '产物结果', titleEn: 'Generated Artifacts', type: 'ok', lowSignal: true },
        );
    }

    return makeActivity(
        lang,
        'execution',
        normalized,
        { titleZh: '执行记录', titleEn: 'Execution Record' },
    );
}

export function humanizeLogMessage(message: string, lang: HumanizeLang): string | null {
    return describeNodeActivity(message, lang)?.text || null;
}

export function getMeaningfulActivityHighlights(
    logs: HumanizeLogEntry[] = [],
    lang: HumanizeLang,
    options?: { nodeType?: string; status?: string; limit?: number },
): ActivityDescriptor[] {
    const limit = options?.limit || 3;
    const ordered = [...logs].sort((a, b) => Number(b.ts || 0) - Number(a.ts || 0));
    const seen = new Set<string>();
    const result: ActivityDescriptor[] = [];

    for (const log of ordered) {
        const descriptor = describeNodeActivity(String(log.msg || ''), lang, options);
        if (!descriptor || descriptor.lowSignal) continue;
        if (['task', 'skills', 'phase', 'progress'].includes(descriptor.category)) continue;
        const key = `${descriptor.title}::${descriptor.text}`;
        if (seen.has(key)) continue;
        seen.add(key);
        result.push(descriptor);
        if (result.length >= limit) break;
    }

    return result;
}

export function getStructuredOutputSections(
    text: string,
    options: { lang: HumanizeLang; nodeType?: string; status?: string },
): StructuredOutputSection[] {
    const normalized = cleanBlock(text);
    if (!normalized) return [];

    if (looksLikeAnalystHandoff(normalized)) {
        return ANALYST_TAGS
            .map((meta) => {
                const value = extractTagBlock(normalized, meta.tag);
                if (!value) return null;
                return {
                    title: options.lang === 'zh' ? meta.titleZh : meta.titleEn,
                    text: formatStructuredText(value, options.lang),
                    type: meta.type,
                } satisfies StructuredOutputSection;
            })
            .filter((section): section is StructuredOutputSection => Boolean(section?.text));
    }

    const reviewer = parseReviewerPayload(normalized);
    if (reviewer) {
        return [
            {
                title: options.lang === 'zh' ? '审查结论' : 'Review Verdict',
                text: reviewer.verdict
                    ? cleanInline(reviewer.verdict)
                    : tr(options.lang, '已生成结构化结论。', 'A structured verdict was generated.'),
                type: reviewer.verdict && ['reject', 'rejected', 'needs_fix', 'blocked'].includes(reviewer.verdict.toLowerCase()) ? 'error' : 'ok',
            },
            {
                title: options.lang === 'zh' ? '阻塞问题' : 'Blocking Issues',
                text: formatList(reviewer.blockingIssues, options.lang, '当前未列出阻塞问题。', 'No blocking issues were listed.'),
                type: reviewer.blockingIssues.length > 0 ? 'error' : 'ok',
            },
            {
                title: options.lang === 'zh' ? '必须修改项' : 'Required Changes',
                text: formatList(reviewer.requiredChanges, options.lang, '当前未列出整改要求。', 'No required changes were listed.'),
                type: reviewer.requiredChanges.length > 0 ? 'info' : 'sys',
            },
            {
                title: options.lang === 'zh' ? '验收标准' : 'Acceptance Criteria',
                text: formatList(reviewer.acceptanceCriteria, options.lang, '当前未列出验收标准。', 'No acceptance criteria were listed.'),
                type: 'sys',
            },
        ];
    }

    if (looksLikeCode(normalized) || looksLikeGenericStructuredData(normalized)) {
        return [{
            title: options.lang === 'zh' ? '产物结果' : 'Generated Artifacts',
            text: summarizeArtifactOutput(normalized, options.nodeType || 'builder', options.status || '', options.lang),
            type: ['failed', 'error', 'blocked'].includes(String(options.status || '').trim().toLowerCase()) ? 'error' : 'ok',
        }];
    }

    return [];
}

export function buildReadableCurrentWork(input: NodeHumanizeInput): string {
    const {
        lang,
        nodeType,
        status,
        phase = '',
        taskDescription = '',
        outputSummary = '',
        lastOutput = '',
        loadedSkills = [],
        logs = [],
        durationText = '',
    } = input;

    const primaryOutput = cleanBlock(outputSummary) || cleanBlock(lastOutput);

    let mainText = '';
    if (primaryOutput && looksLikeAnalystHandoff(primaryOutput)) {
        mainText = summarizeAnalystHandoff(primaryOutput, lang);
    } else if (primaryOutput && looksLikeReviewerPayload(primaryOutput)) {
        mainText = summarizeReviewerPayload(primaryOutput, lang);
    } else if (cleanInline(outputSummary) && !looksLikeMachineOutput(outputSummary)) {
        mainText = cleanInline(outputSummary);
    } else if (primaryOutput && (looksLikeCode(primaryOutput) || looksLikeGenericStructuredData(primaryOutput))) {
        mainText = summarizeArtifactOutput(primaryOutput, nodeType, status, lang);
    } else {
        mainText = baseNodeSentence(nodeType, status, lang, durationText);
    }

    const details: string[] = [];
    const formattedPhase = formatPhaseLabel(phase, lang);
    if (formattedPhase) {
        details.push(tr(lang, `当前阶段：${formattedPhase}。`, `Current phase: ${formattedPhase}.`));
    }

    if (taskDescription && mainText.length < 40) {
        details.push(tr(
            lang,
            `当前任务聚焦：${clip(cleanInline(taskDescription), 120)}`,
            `Task focus: ${clip(cleanInline(taskDescription), 140)}`,
        ));
    }

    const skills = dedupeList(loadedSkills.map(formatSkillLabel));
    if (skills.length > 0) {
        details.push(tr(
            lang,
            `已加载技能：${skills.slice(0, 4).join('、')}${skills.length > 4 ? ` 等 ${skills.length} 个` : ''}。`,
            `Skills loaded: ${skills.slice(0, 4).join(', ')}${skills.length > 4 ? ` and ${skills.length - 4} more` : ''}.`,
        ));
    }

    const highlights = getMeaningfulActivityHighlights(logs, lang, { nodeType, status, limit: 3 });
    if (highlights.length > 0) {
        details.push([
            tr(lang, '最近已经完成：', 'Recently completed:'),
            ...highlights.map((item, index) => `${index + 1}. ${item.text}`),
        ].join('\n'));
    }

    const latestLog = getLatestHumanLog(logs, lang);
    if (
        latestLog
        && highlights.length === 0
        && !mainText.includes(latestLog)
        && !details.some((item) => item.includes(latestLog))
    ) {
        details.push(tr(lang, `最近动作：${latestLog}`, `Latest action: ${latestLog}`));
    }

    return [mainText, ...details].filter(Boolean).join('\n');
}

export function buildReadableNodePreview(input: NodeHumanizeInput): string {
    const summary = buildReadableCurrentWork(input).replace(/\s*\n+\s*/g, ' ');
    return clip(summary, input.lang === 'zh' ? 88 : 132);
}
