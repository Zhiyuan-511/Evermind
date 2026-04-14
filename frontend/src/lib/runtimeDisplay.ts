export const OPENCLAW_UI_ENABLED = process.env.NEXT_PUBLIC_ENABLE_OPENCLAW_UI === '1';

export function normalizeRuntimeModeForDisplay(runtimeMode?: string | null): string {
    const normalized = String(runtimeMode || '').trim().toLowerCase();
    if (!normalized) return 'local';
    if (normalized === 'openclaw' && !OPENCLAW_UI_ENABLED) return 'local';
    return normalized;
}

export function runtimeLabelForDisplay(runtimeMode?: string | null): string {
    const normalized = normalizeRuntimeModeForDisplay(runtimeMode);
    if (!normalized || normalized === 'local') return 'LOCAL';
    if (normalized === 'openclaw') return 'OPENCLAW';
    return normalized.toUpperCase();
}
