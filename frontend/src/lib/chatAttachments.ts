import type { ChatAttachment } from '@/lib/types';

const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.bmp', '.avif']);

export const MAX_CHAT_ATTACHMENTS = 8;
export const MAX_CHAT_ATTACHMENT_BYTES = 12 * 1024 * 1024;

function clampText(value: string, limit: number): string {
    return value.length > limit ? `${value.slice(0, Math.max(0, limit - 3))}...` : value;
}

function getExtension(name: string): string {
    const match = String(name || '').toLowerCase().match(/(\.[a-z0-9]+)$/);
    return match ? match[1] : '';
}

export function inferAttachmentKind(name: string, mimeType: string, rawKind?: unknown): ChatAttachment['kind'] {
    if (rawKind === 'image' || rawKind === 'file') return rawKind;
    const normalizedMime = String(mimeType || '').trim().toLowerCase();
    if (normalizedMime.startsWith('image/')) return 'image';
    return IMAGE_EXTENSIONS.has(getExtension(name)) ? 'image' : 'file';
}

export function formatAttachmentSize(bytes: number): string {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function normalizeChatAttachment(value: unknown): ChatAttachment | null {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const record = value as Record<string, unknown>;
    const name = String(record.name || '').trim().slice(0, 220);
    const path = String(record.path || '').trim().slice(0, 2000);
    if (!name || !path) return null;
    const mimeType = String(record.mimeType ?? record.mime_type ?? 'application/octet-stream').trim().slice(0, 160) || 'application/octet-stream';
    return {
        id: String(record.id || `${Date.now().toString(36)}_${name}`).trim().slice(0, 120),
        name,
        path,
        mimeType,
        size: Math.max(0, Number(record.size || 0)),
        kind: inferAttachmentKind(name, mimeType, record.kind),
        previewUrl: typeof record.previewUrl === 'string'
            ? record.previewUrl
            : typeof record.preview_url === 'string'
                ? record.preview_url
                : undefined,
    };
}

export function dedupeChatAttachments(items: ChatAttachment[]): ChatAttachment[] {
    const seen = new Set<string>();
    return items.reduce<ChatAttachment[]>((acc, item) => {
        const key = `${item.name}::${item.size}::${item.mimeType}`;
        if (!item.path || seen.has(key)) return acc;
        seen.add(key);
        acc.push(item);
        return acc;
    }, []);
}

export function defaultGoalFromAttachments(lang: 'en' | 'zh'): string {
    return lang === 'zh'
        ? '请基于我附加的文件和图片继续完成本轮任务。'
        : 'Please continue this task using my attached files and images.';
}

export function buildMessageContentForHistory(content: string, attachments: ChatAttachment[] = [], maxLength = 1800): string {
    const trimmed = String(content || '').trim();
    if (!attachments.length) return clampText(trimmed, maxLength);
    const lines = attachments.slice(0, MAX_CHAT_ATTACHMENTS).map((attachment) => {
        const sizeLabel = attachment.size > 0 ? `, ${formatAttachmentSize(attachment.size)}` : '';
        const pathLabel = clampText(attachment.path, 180);
        return `- ${attachment.name} [${attachment.kind}, ${attachment.mimeType}${sizeLabel}] @ ${pathLabel}`;
    });
    const block = `[Attached files]\n${lines.join('\n')}`;
    return clampText(trimmed ? `${trimmed}\n\n${block}` : block, maxLength);
}
