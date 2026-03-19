'use client';

import { useCallback, useEffect, useState } from 'react';
import type { RunReportRecord, RunSubtaskReport } from '@/lib/types';

// ── Constants ──
const RUN_REPORTS_STORAGE_KEY = 'evermind-run-reports-v1';
const MAX_RUN_REPORTS = 60;

// ── Helpers ──
function normalizeRunReport(raw: unknown): RunReportRecord | null {
    if (!raw || typeof raw !== 'object') return null;
    const value = raw as Partial<RunReportRecord>;
    if (!value.id || typeof value.id !== 'string') return null;
    if (!value.goal || typeof value.goal !== 'string') return null;
    const difficulty = (value.difficulty === 'simple' || value.difficulty === 'pro') ? value.difficulty : 'standard';
    const subtasks = Array.isArray(value.subtasks) ? (value.subtasks as Array<Partial<RunSubtaskReport>>) : [];
    return {
        id: value.id,
        createdAt: typeof value.createdAt === 'number' ? value.createdAt : Date.now(),
        goal: value.goal.slice(0, 1200),
        difficulty,
        success: Boolean(value.success),
        totalSubtasks: Math.max(0, Number(value.totalSubtasks || 0)),
        completed: Math.max(0, Number(value.completed || 0)),
        failed: Math.max(0, Number(value.failed || 0)),
        totalRetries: Math.max(0, Number(value.totalRetries || 0)),
        durationSeconds: Math.max(0, Number(value.durationSeconds || 0)),
        previewUrl: typeof value.previewUrl === 'string' ? value.previewUrl : undefined,
        subtasks: subtasks.slice(0, 30).map((st, idx: number) => ({
            id: String(st?.id ?? idx + 1),
            agent: String(st?.agent || 'agent'),
            status: String(st?.status || 'unknown'),
            retries: Math.max(0, Number(st?.retries || 0)),
            task: String(st?.task || '').slice(0, 1200),
            outputPreview: String(st?.outputPreview || '').slice(0, 2000),
            error: String(st?.error || '').slice(0, 800),
            durationSeconds: Number.isFinite(Number(st?.durationSeconds)) ? Math.max(0, Number(st.durationSeconds)) : undefined,
            startedAt: Number.isFinite(Number(st?.startedAt)) ? Number(st.startedAt) : undefined,
            endedAt: Number.isFinite(Number(st?.endedAt)) ? Number(st.endedAt) : undefined,
            timelineEvents: Array.isArray(st?.timelineEvents)
                ? st.timelineEvents
                    .filter((item: unknown) => typeof item === 'string' && item.trim())
                    .map((item: string) => item.slice(0, 260))
                    .slice(0, 30)
                : undefined,
        })),
    };
}

function reportRichness(report: RunReportRecord): number {
    let score = report.subtasks.length * 10;
    if (report.previewUrl) score += 3;
    if (report.taskId) score += 2;
    if (report.success) score += 1;
    score += report.subtasks.reduce((acc, st) => {
        let next = acc;
        if (st.outputPreview) next += 2;
        if (st.timelineEvents?.length) next += 2;
        if (st.workSummary?.length) next += 2;
        if (st.filesCreated?.length) next += 1;
        return next;
    }, 0);
    return score;
}

function mergeRunReports(...groups: RunReportRecord[][]): RunReportRecord[] {
    const byId = new Map<string, RunReportRecord>();
    for (const group of groups) {
        for (const report of group) {
            const existing = byId.get(report.id);
            if (!existing || reportRichness(report) >= reportRichness(existing)) {
                byId.set(report.id, report);
            }
        }
    }
    return Array.from(byId.values())
        .sort((a, b) => b.createdAt - a.createdAt)
        .slice(0, MAX_RUN_REPORTS);
}

// ── Hook ──
export interface UseRunReportsReturn {
    runReports: RunReportRecord[];
    addReport: (report: RunReportRecord) => void;
    deleteReport: (reportId: string) => void;
    clearReports: () => void;
}

export function useRunReports(): UseRunReportsReturn {
    const [runReports, setRunReports] = useState<RunReportRecord[]>([]);

    // Load from localStorage
    useEffect(() => {
        if (typeof window === 'undefined') return;
        try {
            const raw = window.localStorage.getItem(RUN_REPORTS_STORAGE_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            const normalized = (Array.isArray(parsed) ? parsed : [])
                .map((item: unknown) => normalizeRunReport(item))
                .filter((item): item is RunReportRecord => !!item)
                .sort((a, b) => b.createdAt - a.createdAt)
                .slice(0, MAX_RUN_REPORTS);
            setRunReports(normalized);
        } catch {
            setRunReports([]);
        }
    }, []);

    // Merge with backend
    useEffect(() => {
        let cancelled = false;
        void (async () => {
            try {
                const { getReports } = await import('@/lib/api');
                const { reports } = await getReports();
                if (cancelled || reports.length === 0) return;
                setRunReports((prev) => mergeRunReports(prev, reports));
            } catch { /* backend unavailable */ }
        })();
        return () => { cancelled = true; };
    }, []);

    // Persist to localStorage
    useEffect(() => {
        if (typeof window === 'undefined') return;
        try {
            window.localStorage.setItem(RUN_REPORTS_STORAGE_KEY, JSON.stringify(runReports));
        } catch { /* ignore */ }
    }, [runReports]);

    const addReport = useCallback((report: RunReportRecord) => {
        setRunReports((prev) => mergeRunReports([report], prev));
    }, []);

    const deleteReport = useCallback((reportId: string) => {
        setRunReports((prev) => prev.filter((item) => item.id !== reportId));
    }, []);

    const clearReports = useCallback(() => {
        setRunReports([]);
    }, []);

    return { runReports, addReport, deleteReport, clearReports };
}
