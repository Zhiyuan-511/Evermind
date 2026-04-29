'use client';

import { useState, useEffect, useCallback } from 'react';
import { NODE_TYPES } from '@/lib/types';

interface SettingsModalProps {
    open: boolean;
    onClose: () => void;
    lang: 'en' | 'zh';
    onLangChange: (lang: 'en' | 'zh') => void;
    theme: 'dark' | 'light';
    onThemeChange: (theme: 'dark' | 'light') => void;
    connected: boolean;
    wsUrl: string;
    onWsUrlChange: (url: string) => void;
    wsRef?: React.MutableRefObject<WebSocket | null>;
}

interface ApiKeys {
    kimi: string;
    gemini: string;
    openai: string;
    claude: string;
    deepseek: string;
    qwen: string;
    // v5.8.6: MiniMax (incl. m2.7-highspeed relay variants) and Zhipu (GLM-4.x)
    // are in MODEL_REGISTRY but were missing from Settings UI — user had no way
    // to paste their key.
    minimax: string;
    zhipu: string;
    doubao: string;
    yi: string;
    aigate: string;   // v5.8.6: private-relay.example multi-model relay (sk-ag-*)
    // v5.8.6: optional secondary keys per provider — enable concurrent-node
    // load balancing (e.g. imagegen + spritesheet + builder1 + builder2 running
    // in parallel stop fighting for the same key's rate limit).
    kimi_2?: string;
    gemini_2?: string;
    openai_2?: string;
    claude_2?: string;
    deepseek_2?: string;
    qwen_2?: string;
    minimax_2?: string;
    zhipu_2?: string;
    doubao_2?: string;
    yi_2?: string;
    aigate_2?: string;
}

interface ModelCatalogItem {
    id: string;
    provider: string;
}

interface RelayCatalogItem {
    id: string;
    label: string;
    provider: string;
    api_style: string;
    default_base_url?: string;
    default_models?: string[];
    description?: string;
}

interface RelayEndpointRecord {
    id: string;
    name: string;
    base_url: string;
    provider: string;
    api_style: string;
    models: string[];
    enabled: boolean;
    template_id?: string;
    last_test?: {
        success?: boolean;
        connectivity_ok?: boolean;
        streaming_ok?: boolean;
        tool_calling_ok?: boolean;
        builder_profile_ok?: boolean;
        latency_ms?: number;
        error?: string;
    };
}

interface RelayDraft {
    templateId: string;
    name: string;
    baseUrl: string;
    apiKey: string;
    models: string;
}

type AnalystCrawlIntensity = 'off' | 'low' | 'medium' | 'high';

type TabId = 'conn' | 'perm' | 'ui' | 'quality' | 'nodes' | 'speed' | 'cli';

const TABS: { id: TabId; label_en: string; label_zh: string }[] = [
    { id: 'conn', label_en: 'Connection', label_zh: '连接' },
    { id: 'perm', label_en: 'Permissions', label_zh: '权限' },
    { id: 'quality', label_en: 'Quality', label_zh: '验收策略' },
    { id: 'nodes', label_en: 'Node Models', label_zh: '节点模型' },
    { id: 'speed', label_en: 'Speed Test', label_zh: '模型测速' },
    { id: 'cli', label_en: 'CLI Mode', label_zh: 'CLI 模式' },
    { id: 'ui', label_en: 'Interface', label_zh: '界面' },
];

interface CLIDetectResult {
    available: boolean;
    name: string;
    display_name: string;
    path?: string;
    version?: string;
    supports_json?: boolean;
    supports_file_ops?: boolean;
    supports_streaming?: boolean;
    is_extra?: boolean;
    error?: string;
}

interface CLITestResult {
    success: boolean;
    latency_s?: number;
    output_preview?: string;
    error?: string;
}

interface CLIModelOption {
    id: string;
    name: string;
}

interface CLINodeOverride {
    cli: string;
    model: string;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8765';
const NODE_MODEL_PRIORITY_SLOTS = [0, 1, 2] as const;
const NODE_MODEL_CONFIG_ROLES = [
    'router',
    'planner',
    'analyst',
    'uidesign',
    'scribe',
    'builder',
    'polisher',
    'patcher',
    'reviewer',
    'debugger',
    'deployer',
    'imagegen',
    'spritesheet',
    'assetimport',
    'merger',
] as const;
const FALLBACK_MODEL_CATALOG: ModelCatalogItem[] = [
    { id: 'gpt-5.4', provider: 'openai' },
    { id: 'gpt-4.1', provider: 'openai' },
    { id: 'gpt-4o', provider: 'openai' },
    { id: 'o3', provider: 'openai' },
    { id: 'claude-4-sonnet', provider: 'anthropic' },
    { id: 'claude-4-opus', provider: 'anthropic' },
    { id: 'claude-3.5-sonnet', provider: 'anthropic' },
    { id: 'gemini-2.5-pro', provider: 'google' },
    { id: 'gemini-2.0-flash', provider: 'google' },
    { id: 'deepseek-v3', provider: 'deepseek' },
    { id: 'deepseek-r1', provider: 'deepseek' },
    { id: 'kimi', provider: 'kimi' },
    { id: 'kimi-k2.5', provider: 'kimi' },
    { id: 'kimi-coding', provider: 'kimi' },
    { id: 'qwen-max', provider: 'qwen' },
];
const PROVIDER_LABELS: Record<string, string> = {
    openai: 'OpenAI',
    anthropic: 'Claude',
    google: 'Gemini',
    deepseek: 'DeepSeek',
    kimi: 'Kimi',
    qwen: 'Qwen',
    ollama: 'Ollama',
    relay: 'Relay',
};
const DEFAULT_RELAY_DRAFT: RelayDraft = {
    templateId: 'openai_compat',
    name: '',
    baseUrl: '',
    apiKey: '',
    models: 'gpt-5.4',
};

// ── Security: mask API key for display ──
function maskKey(key: string): string {
    if (!key) return '';
    if (key.length <= 8) return '●'.repeat(key.length);
    return key.slice(0, 4) + '●'.repeat(Math.min(key.length - 8, 20)) + key.slice(-4);
}

// ── Security: sanitize pasted content ──
// v7.4: was stripping `<>"'&` which broke relay baseUrl query strings
// (?key=...&token=...) and any apiKey containing those characters. Now we
// only kill HTML tag bytes and angle brackets — quotes / `&` / `'` are
// legitimate inside URLs and credentials, and React already escapes them
// when rendering, so they're not an XSS vector here.
function sanitizeInput(value: string): string {
    return value.replace(/<[^>]*>/g, '').replace(/[<>]/g, '').trim();
}

function sanitizeMultilineInput(value: string): string {
    return value.replace(/<[^>]*>/g, '');
}

function normalizeNodeModelPreferences(value: unknown): Record<string, string[]> {
    if (!value || typeof value !== 'object') return {};
    const normalized: Record<string, string[]> = {};
    for (const [rawRole, rawChain] of Object.entries(value as Record<string, unknown>)) {
        const role = String(rawRole || '').trim();
        if (!role) continue;
        const source = Array.isArray(rawChain)
            ? rawChain
            : typeof rawChain === 'string'
                ? rawChain.split(',')
                : [];
        const chain = source
            .map(item => String(item || '').trim())
            .filter(Boolean)
            .filter((item, index, arr) => arr.indexOf(item) === index)
            .slice(0, 6);
        if (chain.length > 0) normalized[role] = chain;
    }
    return normalized;
}

function normalizeRelayModels(value: unknown): string[] {
    if (!Array.isArray(value)) return [];
    return value
        .map((item) => String(item || '').trim())
        .filter(Boolean)
        .filter((item, index, arr) => arr.indexOf(item) === index)
        .slice(0, 24);
}

function parseRelayModels(value: string): string[] {
    return value
        .split(/[\n,]/)
        .map((item) => String(item || '').trim())
        .filter(Boolean)
        .filter((item, index, arr) => arr.indexOf(item) === index)
        .slice(0, 24);
}

export default function SettingsModal({
    open, onClose, lang, onLangChange, theme, onThemeChange,
    connected, wsUrl, onWsUrlChange, wsRef,
}: SettingsModalProps) {
    const [tab, setTab] = useState<TabId>('conn');
    const [apiKeys, setApiKeys] = useState<ApiKeys>({
        kimi: '', gemini: '', openai: '', claude: '', deepseek: '', qwen: '',
        // v5.8.6: MiniMax / Zhipu (GLM) / Doubao / Yi were missing from UI —
        // every provider that has entries in MODEL_REGISTRY must be listed so
        // users can paste keys for any registered model.
        minimax: '', zhipu: '', doubao: '', yi: '',
        aigate: '',   // v5.8.6: private-relay.example relay
    });
    const [apiBases, setApiBases] = useState<Record<string, string>>({
        openai: '', claude: '', gemini: '', kimi: '', deepseek: '', qwen: '',
        // v5.8.6: add new providers
        minimax: '', zhipu: '', doubao: '', yi: '',
        aigate: 'https://llm.private-relay.example/v1',  // default to private-relay
    });
    const [autoL2, setAutoL2] = useState(true);
    const [l4Pass, setL4Pass] = useState('godmode');
    const [allowedDirs, setAllowedDirs] = useState('~/Desktop, ~/Documents');
    const [maxFileSize, setMaxFileSize] = useState('50MB');
    const [saving, setSaving] = useState(false);
    const [saveStatus, setSaveStatus] = useState<'idle' | 'success' | 'error'>('idle');
    // Quality settings
    const [smokeEnabled, setSmokeEnabled] = useState(true);
    const [browserHeadful, setBrowserHeadful] = useState(false);
    const [forceVisibleReview, setForceVisibleReview] = useState(false);
    const [maxRetries, setMaxRetries] = useState(3);
    const [browserResearch, setBrowserResearch] = useState(false);
    const [comfyUiUrl, setComfyUiUrl] = useState('');
    const [comfyWorkflowTemplate, setComfyWorkflowTemplate] = useState('');
    const [imageBackendAvailable, setImageBackendAvailable] = useState(false);
    // v6.2 (maintainer 2026-04-20): direct image provider (preferred over ComfyUI).
    const [imgProvider, setImgProvider] = useState<'' | 'doubao-image' | 'seedream' | 'tongyi' | 'flux-fal' | 'openai-compat'>('');
    const [imgApiKey, setImgApiKey] = useState('');
    const [imgBaseUrl, setImgBaseUrl] = useState('');
    const [imgDefaultModel, setImgDefaultModel] = useState('');
    const [imgDefaultSize, setImgDefaultSize] = useState('1024x1024');
    const [imgMaxImages, setImgMaxImages] = useState(10);
    const [imgAutoCrop, setImgAutoCrop] = useState(true);
    const [imgTestLoading, setImgTestLoading] = useState(false);
    const [imgTestResult, setImgTestResult] = useState<{ ok: boolean; latency_ms?: number; image_base64?: string; error?: string } | null>(null);
    const [nodeModelPreferences, setNodeModelPreferences] = useState<Record<string, string[]>>({});
    const [thinkingDepth, setThinkingDepth] = useState<'fast' | 'deep'>('deep');
    // v7.7 (maintainer 2026-04-27): user-controlled reviewer reject budget. 1 = single-loop
    // closure (v6.7 default, fastest); higher = stricter quality gate at cost of
    // longer runs. Capped at 5 in normal mode (Ultra mode goes to 10 separately).
    const [reviewerMaxRejections, setReviewerMaxRejections] = useState<number>(1);
    // v6.1.3 (maintainer 2026-04-18): dedicated language toggle for node walkthrough reports.
    // "" = inherit UI language; "zh"/"en" = force that language for reports.
    const [walkthroughLang, setWalkthroughLang] = useState<'' | 'zh' | 'en'>('');
    // v6.1.10 (maintainer 2026-04-19): when the user configured TWO API keys for
    // the primary builder provider (e.g. kimi + kimi_2), let parallel peer
    // builders share the preferred first model and round-robin the keys
    // instead of falling back to the second configured model.
    const [peerBuildersShareModelWhenMultikey, setPeerBuildersShareModelWhenMultikey] = useState<boolean>(true);
    const [modelCatalog, setModelCatalog] = useState<ModelCatalogItem[]>([]);
    const [analystPreferredSites, setAnalystPreferredSites] = useState('');
    const [analystCrawlIntensity, setAnalystCrawlIntensity] = useState<AnalystCrawlIntensity>('medium');
    const [analystUseScrapling, setAnalystUseScrapling] = useState(true);
    const [analystEnableQuerySearch, setAnalystEnableQuerySearch] = useState(true);
    const [relayCatalog, setRelayCatalog] = useState<RelayCatalogItem[]>([]);
    const [relayEndpoints, setRelayEndpoints] = useState<RelayEndpointRecord[]>([]);
    const [relayDraft, setRelayDraft] = useState<RelayDraft>(DEFAULT_RELAY_DRAFT);
    const [relayStatus, setRelayStatus] = useState('');
    const [relaySaving, setRelaySaving] = useState(false);
    const [relayActionId, setRelayActionId] = useState('');
    // Speed test state
    const [speedTestRunning, setSpeedTestRunning] = useState(false);
    const [speedTestResults, setSpeedTestResults] = useState<Record<string, { ok: boolean; latency_ms: number; error: string; provider: string }>>({});
    const [speedTestProgress, setSpeedTestProgress] = useState('');
    // Track which keys the user is actively editing (show real value)
    const [editingKey, setEditingKey] = useState<string | null>(null);
    // Track which keys have been loaded from backend (display as masked)
    const [loadedKeys, setLoadedKeys] = useState<Set<string>>(new Set());
    // CLI mode state
    const [cliEnabled, setCliEnabled] = useState(false);
    const [cliUltraMode, setCliUltraMode] = useState(false);
    const [cliPreferred, setCliPreferred] = useState('');
    const [cliPreferredModel, setCliPreferredModel] = useState('');
    const [cliDetected, setCliDetected] = useState<Record<string, CLIDetectResult>>({});
    const [cliNodeOverrides, setCliNodeOverrides] = useState<Record<string, CLINodeOverride>>({});
    const [cliDetecting, setCliDetecting] = useState(false);
    const [cliTestResults, setCliTestResults] = useState<Record<string, CLITestResult>>({});
    const [cliTestingAll, setCliTestingAll] = useState(false);
    const [cliModelOptions, setCliModelOptions] = useState<Record<string, CLIModelOption[]>>({});

    const loadRelayState = useCallback(async () => {
        try {
            const [catalogResp, listResp] = await Promise.all([
                fetch(`${API_BASE}/api/relay/catalog`, { credentials: 'omit' }),
                fetch(`${API_BASE}/api/relay/list`, { credentials: 'omit' }),
            ]);
            if (catalogResp.ok) {
                const catalogJson = await catalogResp.json();
                const catalog = Array.isArray(catalogJson.templates) ? catalogJson.templates : [];
                setRelayCatalog(
                    catalog
                        .map((item: Record<string, unknown>) => ({
                            id: String(item.id || '').trim(),
                            label: String(item.label || '').trim(),
                            provider: String(item.provider || '').trim(),
                            api_style: String(item.api_style || '').trim(),
                            default_base_url: String(item.default_base_url || '').trim(),
                            default_models: normalizeRelayModels(item.default_models),
                            description: String(item.description || '').trim(),
                        }))
                        .filter((item: RelayCatalogItem) => item.id.length > 0)
                );
            }
            if (listResp.ok) {
                const listJson = await listResp.json();
                const relays = Array.isArray(listJson.relays) ? listJson.relays : [];
                setRelayEndpoints(
                    relays
                        .map((item: Record<string, unknown>) => ({
                            id: String(item.id || '').trim(),
                            name: String(item.name || '').trim(),
                            base_url: String(item.base_url || '').trim(),
                            provider: String(item.provider || '').trim(),
                            api_style: String(item.api_style || '').trim(),
                            models: normalizeRelayModels(item.models),
                            enabled: Boolean(item.enabled ?? true),
                            template_id: String(item.template_id || '').trim(),
                            last_test: item.last_test && typeof item.last_test === 'object'
                                ? item.last_test as RelayEndpointRecord['last_test']
                                : undefined,
                        }))
                        .filter((item: RelayEndpointRecord) => item.id.length > 0)
                );
            }
        } catch {
            // Keep settings usable even if relay endpoints fail to load.
        }
    }, []);

    // v5.8.6: refetch /api/models when keys change anywhere (Save button,
    // external dispatch). Scoped only to the modelCatalog so we don't
    // re-pull the whole settings payload on every event.
    useEffect(() => {
        if (!open) return;
        const handler = async () => {
            try {
                const modelsResp = await fetch(`${API_BASE}/api/models`, { cache: 'no-store' });
                if (!modelsResp.ok) return;
                const modelsData: { models?: Array<{ id: string; provider: string; has_key?: boolean }> } = await modelsResp.json();
                const filtered = (modelsData.models || [])
                    .filter((m) => m.has_key === true)
                    .map((m) => ({ id: m.id, provider: m.provider }));
                if (filtered.length > 0) setModelCatalog(filtered);
            } catch { /* ignore */ }
        };
        window.addEventListener('evermind-models-changed', handler);
        return () => window.removeEventListener('evermind-models-changed', handler);
    }, [open]);

    // Load settings from backend on open — only load has_keys flags, NOT the masked values
    useEffect(() => {
        if (!open) return;
        void loadRelayState();
        fetch(`${API_BASE}/api/settings`, { credentials: 'omit' })
            .then(r => r.json())
            .then(async data => {
                // Backend returns has_keys: {openai_api_key: true, ...}
                // We use this to show which keys are already configured
                if (data.has_keys) {
                    const loaded = new Set<string>();
                    // Backend returns has_keys with short names: {openai: true, kimi: true, ...}
                    const keyMapping: Record<string, string> = {
                        openai_api_key: 'openai', openai: 'openai',
                        anthropic_api_key: 'claude', anthropic: 'claude',
                        gemini_api_key: 'gemini', gemini: 'gemini',
                        kimi_api_key: 'kimi', kimi: 'kimi',
                        deepseek_api_key: 'deepseek', deepseek: 'deepseek',
                        qwen_api_key: 'qwen', qwen: 'qwen',
                    };
                    for (const [backendKey, hasKey] of Object.entries(data.has_keys)) {
                        const frontendKey = keyMapping[backendKey];
                        if (frontendKey && hasKey) loaded.add(frontendKey);
                    }
                    setLoadedKeys(loaded);
                }
                // Load quality settings
                if (typeof data.builder_enable_browser === 'boolean') setBrowserResearch(data.builder_enable_browser);
                if (typeof data.tester_run_smoke === 'boolean') setSmokeEnabled(data.tester_run_smoke);
                if (typeof data.browser_headful === 'boolean') setBrowserHeadful(data.browser_headful);
                if (typeof data.reviewer_tester_force_headful === 'boolean') setForceVisibleReview(data.reviewer_tester_force_headful);
                if (typeof data.max_retries === 'number') setMaxRetries(Math.max(1, Math.min(8, data.max_retries)));
                if (data.image_generation && typeof data.image_generation === 'object') {
                    const imageCfg = data.image_generation as Record<string, unknown>;
                    if (typeof imageCfg.comfyui_url === 'string') setComfyUiUrl(imageCfg.comfyui_url);
                    if (typeof imageCfg.workflow_template === 'string') setComfyWorkflowTemplate(imageCfg.workflow_template);
                    // v6.2 direct provider fields
                    if (typeof imageCfg.provider === 'string') {
                        const p = String(imageCfg.provider).trim().toLowerCase();
                        if (['doubao-image', 'seedream', 'tongyi', 'flux-fal', 'openai-compat', ''].includes(p)) {
                            setImgProvider(p as typeof imgProvider);
                        }
                    }
                    if (typeof imageCfg.api_key === 'string') setImgApiKey(imageCfg.api_key);
                    if (typeof imageCfg.base_url === 'string') setImgBaseUrl(imageCfg.base_url);
                    if (typeof imageCfg.default_model === 'string') setImgDefaultModel(imageCfg.default_model);
                    if (typeof imageCfg.default_size === 'string' && imageCfg.default_size) setImgDefaultSize(imageCfg.default_size);
                    if (typeof imageCfg.max_images_per_run === 'number') setImgMaxImages(Math.max(1, Math.min(40, imageCfg.max_images_per_run)));
                    if (typeof imageCfg.auto_crop === 'boolean') setImgAutoCrop(imageCfg.auto_crop);
                }
                if (typeof data.image_generation_available === 'boolean') setImageBackendAvailable(data.image_generation_available);
                if (data.node_model_preferences && typeof data.node_model_preferences === 'object') {
                    setNodeModelPreferences(normalizeNodeModelPreferences(data.node_model_preferences));
                }
                if (data.thinking_depth === 'fast' || data.thinking_depth === 'deep') {
                    setThinkingDepth(data.thinking_depth);
                }
                if (typeof data.reviewer_max_rejections === 'number' && data.reviewer_max_rejections >= 0) {
                    setReviewerMaxRejections(Math.min(10, Math.max(0, Math.floor(data.reviewer_max_rejections))));
                }
                if (typeof data.walkthrough_language === 'string') {
                    const wl = String(data.walkthrough_language || '').trim().toLowerCase();
                    if (wl === 'zh' || wl === 'en' || wl === '') {
                        setWalkthroughLang(wl as '' | 'zh' | 'en');
                    }
                }
                if (typeof data.peer_builders_share_model_when_multikey === 'boolean') {
                    setPeerBuildersShareModelWhenMultikey(data.peer_builders_share_model_when_multikey);
                }
                if (data.analyst && typeof data.analyst === 'object') {
                    const analystCfg = data.analyst as Record<string, unknown>;
                    const preferredSites = Array.isArray(analystCfg.preferred_sites)
                        ? analystCfg.preferred_sites
                            .map((item) => String(item || '').trim())
                            .filter(Boolean)
                        : [];
                    setAnalystPreferredSites(preferredSites.join('\n'));
                    const nextIntensity = String(analystCfg.crawl_intensity || 'medium').trim().toLowerCase();
                    if (['off', 'low', 'medium', 'high'].includes(nextIntensity)) {
                        setAnalystCrawlIntensity(nextIntensity as AnalystCrawlIntensity);
                    }
                    if (typeof analystCfg.use_scrapling_when_available === 'boolean') {
                        setAnalystUseScrapling(analystCfg.use_scrapling_when_available);
                    }
                    if (typeof analystCfg.enable_query_search === 'boolean') {
                        setAnalystEnableQuerySearch(analystCfg.enable_query_search);
                    }
                }
                // v5.8.6: prefer /api/models (has_key filter) over data.model_catalog
                // (which is all-registered-models unfiltered). This keeps the
                // Settings → Per-Node Model Fallback dropdown limited to models
                // whose provider actually has a key configured.
                try {
                    const modelsResp = await fetch(`${API_BASE}/api/models`, { cache: 'no-store' });
                    if (modelsResp.ok) {
                        const modelsData: { models?: Array<{ id: string; provider: string; has_key?: boolean }> } = await modelsResp.json();
                        const filtered = (modelsData.models || [])
                            .filter((m) => m.has_key === true)
                            .map((m) => ({ id: m.id, provider: m.provider }));
                        if (filtered.length > 0) {
                            setModelCatalog(filtered);
                        } else if (Array.isArray(data.model_catalog)) {
                            // Fallback to unfiltered catalog only when no key is configured.
                            const catalog: ModelCatalogItem[] = data.model_catalog
                                .map((item: unknown) => ({
                                    id: String((item as Record<string, unknown>).id || '').trim(),
                                    provider: String((item as Record<string, unknown>).provider || '').trim(),
                                }))
                                .filter((item: ModelCatalogItem) => item.id.length > 0);
                            if (catalog.length > 0) setModelCatalog(catalog);
                        }
                    }
                } catch {
                    // If /api/models fails, fall back to the unfiltered list
                    if (Array.isArray(data.model_catalog)) {
                        const catalog: ModelCatalogItem[] = data.model_catalog
                            .map((item: unknown) => ({
                                id: String((item as Record<string, unknown>).id || '').trim(),
                                provider: String((item as Record<string, unknown>).provider || '').trim(),
                            }))
                            .filter((item: ModelCatalogItem) => item.id.length > 0);
                        if (catalog.length > 0) setModelCatalog(catalog);
                    }
                }
                // Load CLI mode settings
                if (data.cli_mode && typeof data.cli_mode === 'object') {
                    const cliCfg = data.cli_mode as Record<string, unknown>;
                    if (typeof cliCfg.enabled === 'boolean') setCliEnabled(cliCfg.enabled);
                    if (typeof cliCfg.ultra_mode === 'boolean') setCliUltraMode(cliCfg.ultra_mode);
                    if (typeof cliCfg.preferred_cli === 'string') setCliPreferred(cliCfg.preferred_cli);
                    if (typeof cliCfg.preferred_model === 'string') setCliPreferredModel(cliCfg.preferred_model);
                    if (cliCfg.detected_clis && typeof cliCfg.detected_clis === 'object') {
                        setCliDetected(cliCfg.detected_clis as Record<string, CLIDetectResult>);
                    }
                    if (cliCfg.node_cli_overrides && typeof cliCfg.node_cli_overrides === 'object') {
                        // Backward compat: convert old string format to new {cli, model} format
                        const raw = cliCfg.node_cli_overrides as Record<string, unknown>;
                        const parsed: Record<string, CLINodeOverride> = {};
                        for (const [k, v] of Object.entries(raw)) {
                            if (typeof v === 'string') {
                                parsed[k] = { cli: v, model: '' };
                            } else if (v && typeof v === 'object') {
                                const obj = v as Record<string, unknown>;
                                parsed[k] = {
                                    cli: String(obj.cli || ''),
                                    model: String(obj.model || ''),
                                };
                            }
                        }
                        setCliNodeOverrides(parsed);
                    }
                }
                // Fetch CLI model options
                try {
                    const modelsResp = await fetch(`${API_BASE}/api/cli/models`, { credentials: 'omit' });
                    if (modelsResp.ok) {
                        const modelsData = await modelsResp.json();
                        if (modelsData.models) setCliModelOptions(modelsData.models);
                    }
                } catch { /* offline */ }
                // Load relay base URLs
                if (data.api_bases && typeof data.api_bases === 'object') {
                    const bases: Record<string, string> = { openai: '', claude: '', gemini: '', kimi: '', deepseek: '', qwen: '' };
                    const baseMapping: Record<string, string> = {
                        openai: 'openai', anthropic: 'claude', gemini: 'gemini',
                        kimi: 'kimi', deepseek: 'deepseek', qwen: 'qwen',
                    };
                    for (const [bk, bv] of Object.entries(data.api_bases)) {
                        const fk = baseMapping[bk] || bk;
                        if (fk in bases && typeof bv === 'string') bases[fk] = bv;
                    }
                    setApiBases(bases);
                }
            }).catch(() => { /* offline */ });
    }, [open, loadRelayState]);

    // Clear sensitive state on close
    const handleClose = useCallback(() => {
        setEditingKey(null);
        setSaveStatus('idle');
        onClose();
    }, [onClose]);

    const updateRelayDraft = useCallback((patch: Partial<RelayDraft>) => {
        setRelayDraft((prev) => ({ ...prev, ...patch }));
    }, []);

    const applyRelayTemplateDraft = useCallback((templateId: string) => {
        const template = relayCatalog.find((item) => item.id === templateId);
        updateRelayDraft({
            templateId,
            name: template?.label && !relayDraft.name ? template.label : relayDraft.name,
            baseUrl: template?.default_base_url && !relayDraft.baseUrl ? template.default_base_url : relayDraft.baseUrl,
            models: template?.default_models && relayDraft.models.trim().length === 0
                ? template.default_models.join(', ')
                : relayDraft.models,
        });
    }, [relayCatalog, relayDraft.baseUrl, relayDraft.models, relayDraft.name, updateRelayDraft]);

    const importOpenAiBaseIntoRelay = useCallback(() => {
        updateRelayDraft({
            templateId: relayDraft.templateId || 'openai_compat',
            name: relayDraft.name || 'OpenAI Relay',
            baseUrl: apiBases.openai || relayDraft.baseUrl,
            models: relayDraft.models || 'gpt-5.4',
        });
    }, [apiBases.openai, relayDraft.baseUrl, relayDraft.models, relayDraft.name, relayDraft.templateId, updateRelayDraft]);

    const addRelayEndpoint = useCallback(async () => {
        const template = relayCatalog.find((item) => item.id === relayDraft.templateId);
        const payload = {
            name: relayDraft.name.trim() || template?.label || 'Relay Endpoint',
            template_id: relayDraft.templateId || '',
            provider: template?.provider || 'openai',
            api_style: template?.api_style || 'openai_compatible',
            base_url: relayDraft.baseUrl.trim(),
            api_key: relayDraft.apiKey.trim(),
            models: parseRelayModels(relayDraft.models),
        };
        setRelaySaving(true);
        setRelayStatus('');
        try {
            const resp = await fetch(`${API_BASE}/api/relay/add`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'omit',
                body: JSON.stringify(payload),
            });
            const json = await resp.json().catch(() => ({}));
            if (!resp.ok || json.error) {
                setRelayStatus(String(json.error || 'Relay add failed'));
                return;
            }
            setRelayDraft(DEFAULT_RELAY_DRAFT);
            setRelayStatus('ok:add');
            await loadRelayState();
        } catch {
            setRelayStatus('Relay add failed');
        } finally {
            setRelaySaving(false);
        }
    }, [loadRelayState, relayCatalog, relayDraft]);

    const testRelayEndpoint = useCallback(async (endpointId: string) => {
        setRelayActionId(endpointId);
        setRelayStatus('');
        try {
            const resp = await fetch(`${API_BASE}/api/relay/test/${endpointId}`, {
                method: 'POST',
                credentials: 'omit',
            });
            const json = await resp.json().catch(() => ({}));
            if (!resp.ok || json.error) {
                setRelayStatus(String(json.error || 'Relay test failed'));
            } else {
                setRelayStatus(json.success ? 'ok:test' : String(json.error || 'Relay test failed'));
            }
            await loadRelayState();
        } catch {
            setRelayStatus('Relay test failed');
        } finally {
            setRelayActionId('');
        }
    }, [loadRelayState]);

    const removeRelayEndpoint = useCallback(async (endpointId: string) => {
        setRelayActionId(endpointId);
        setRelayStatus('');
        try {
            const resp = await fetch(`${API_BASE}/api/relay/${endpointId}`, {
                method: 'DELETE',
                credentials: 'omit',
            });
            const json = await resp.json().catch(() => ({}));
            if (!resp.ok || json.error) {
                setRelayStatus(String(json.error || 'Relay delete failed'));
            } else {
                setRelayStatus('ok:remove');
            }
            await loadRelayState();
        } catch {
            setRelayStatus('Relay delete failed');
        } finally {
            setRelayActionId('');
        }
    }, [loadRelayState]);

    // Available models after save
    const [availableModels, setAvailableModels] = useState<Record<string, string[]>>({});

    const saveToBackend = useCallback(async () => {
        setSaving(true);
        setSaveStatus('idle');

        // Build REST payload — settings.py expects api_keys.{provider}: "sk-..."
        const restKeys: Record<string, string> = {};
        // Build WS payload — server.py update_config expects {provider}_api_key: "sk-..."
        const wsKeys: Record<string, string> = {};
        const mapping: [keyof ApiKeys, string, string][] = [
            ['openai', 'openai', 'openai_api_key'],
            ['claude', 'anthropic', 'anthropic_api_key'],
            ['gemini', 'gemini', 'gemini_api_key'],
            ['kimi', 'kimi', 'kimi_api_key'],
            ['deepseek', 'deepseek', 'deepseek_api_key'],
            ['qwen', 'qwen', 'qwen_api_key'],
            // v5.8.6: new providers
            ['minimax', 'minimax', 'minimax_api_key'],
            ['zhipu', 'zhipu', 'zhipu_api_key'],
            ['doubao', 'doubao', 'doubao_api_key'],
            ['yi', 'yi', 'yi_api_key'],
            ['aigate', 'aigate', 'aigate_api_key'],  // v5.8.6: private-relay.example
        ];
        for (const [uiKey, restKey, wsKey] of mapping) {
            const primary = apiKeys[uiKey];
            if (primary) {
                restKeys[restKey] = primary;
                wsKeys[wsKey] = primary;
            }
            // v5.8.6: persist secondary key under the `_2` suffix — backend pool
            // reads `{provider}_api_key_2` (WS) and `api_keys.{provider}_2` (REST).
            const secondaryKey = `${uiKey}_2` as keyof ApiKeys;
            const secondary = apiKeys[secondaryKey];
            if (secondary) {
                restKeys[`${restKey}_2`] = secondary as string;
                wsKeys[`${wsKey}_2`] = secondary as string;
            }
        }

        try {
            // 1) Save to disk via REST API (settings.py format)
            const resp = await fetch(`${API_BASE}/api/settings/save`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'omit',
                body: JSON.stringify({
                    api_keys: restKeys,
                    api_bases: {
                        openai: apiBases.openai || '',
                        anthropic: apiBases.claude || '',
                        gemini: apiBases.gemini || '',
                        kimi: apiBases.kimi || '',
                        deepseek: apiBases.deepseek || '',
                        qwen: apiBases.qwen || '',
                        // v5.8.6: new provider base URLs (relay / direct)
                        minimax: apiBases.minimax || '',
                        zhipu: apiBases.zhipu || '',
                        doubao: apiBases.doubao || '',
                        yi: apiBases.yi || '',
                        aigate: apiBases.aigate || 'https://llm.private-relay.example/v1',  // v5.8.6
                    },
                    builder_enable_browser: browserResearch,
                    tester_run_smoke: smokeEnabled,
                    browser_headful: browserHeadful,
                    reviewer_tester_force_headful: forceVisibleReview,
                    max_retries: maxRetries,
                    image_generation: {
                        comfyui_url: comfyUiUrl,
                        workflow_template: comfyWorkflowTemplate,
                        provider: imgProvider,
                        api_key: imgApiKey,
                        base_url: imgBaseUrl,
                        default_model: imgDefaultModel,
                        default_size: imgDefaultSize,
                        max_images_per_run: imgMaxImages,
                        auto_crop: imgAutoCrop,
                    },
                    analyst: {
                        preferred_sites: analystPreferredSites
                            .split(/\r?\n/)
                            .map((item) => String(item || '').trim())
                            .filter(Boolean),
                        crawl_intensity: analystCrawlIntensity,
                        use_scrapling_when_available: analystUseScrapling,
                        enable_query_search: analystEnableQuerySearch,
                    },
                    node_model_preferences: nodeModelPreferences,
                    thinking_depth: thinkingDepth,
                    reviewer_max_rejections: reviewerMaxRejections,
                    walkthrough_language: walkthroughLang,
                    peer_builders_share_model_when_multikey: peerBuildersShareModelWhenMultikey,
                    cli_mode: {
                        enabled: cliEnabled,
                        ultra_mode: cliUltraMode,
                        preferred_cli: cliPreferred,
                        preferred_model: cliPreferredModel,
                        detected_clis: cliDetected,
                        node_cli_overrides: cliNodeOverrides,
                    },
                }),
            });

            if (resp.ok) {
                const result = await resp.json();

                // 2) Also update the live WebSocket session so keys take effect immediately
                if (wsRef?.current && wsRef.current.readyState === WebSocket.OPEN) {
                    wsRef.current.send(JSON.stringify({
                        type: 'update_config',
                        config: {
                            ...wsKeys,
                            builder_enable_browser: browserResearch,
                            tester_run_smoke: smokeEnabled,
                            browser_headful: browserHeadful,
                            reviewer_tester_force_headful: forceVisibleReview,
                            max_retries: maxRetries,
                            // v3.0.3: Send UI language so backend reports match user's language
                            ui_language: lang,
                            image_generation: {
                                comfyui_url: comfyUiUrl,
                                workflow_template: comfyWorkflowTemplate,
                                provider: imgProvider,
                                api_key: imgApiKey,
                                base_url: imgBaseUrl,
                                default_model: imgDefaultModel,
                                default_size: imgDefaultSize,
                                max_images_per_run: imgMaxImages,
                                auto_crop: imgAutoCrop,
                            },
                            analyst: {
                                preferred_sites: analystPreferredSites
                                    .split(/\r?\n/)
                                    .map((item) => String(item || '').trim())
                                    .filter(Boolean),
                                crawl_intensity: analystCrawlIntensity,
                                use_scrapling_when_available: analystUseScrapling,
                                enable_query_search: analystEnableQuerySearch,
                            },
                            node_model_preferences: nodeModelPreferences,
                            thinking_depth: thinkingDepth,
                            reviewer_max_rejections: reviewerMaxRejections,
                            walkthrough_language: walkthroughLang,
                            cli_mode: {
                                enabled: cliEnabled,
                                preferred_cli: cliPreferred,
                                preferred_model: cliPreferredModel,
                                node_cli_overrides: cliNodeOverrides,
                            },
                        },
                    }));
                }

                // 3) Show available models
                if (result.available_models) {
                    setAvailableModels(result.available_models);
                }
                if (typeof result.image_generation_available === 'boolean') {
                    setImageBackendAvailable(result.image_generation_available);
                }
                if (result.node_model_preferences && typeof result.node_model_preferences === 'object') {
                    setNodeModelPreferences(normalizeNodeModelPreferences(result.node_model_preferences));
                }

                // v5.8.6: broadcast "models changed" so all other dropdowns
                // (AgentNode canvas dropdown, DirectChatPanel chat picker,
                // Per-Node Model Fallback dropdown) refetch /api/models
                // immediately without needing a page reload.
                if (typeof window !== 'undefined') {
                    window.dispatchEvent(new Event('evermind-models-changed'));
                }

                setSaveStatus('success');
                const loaded = new Set<string>();
                for (const [k, v] of Object.entries(apiKeys)) {
                    if (v) loaded.add(k);
                }
                setLoadedKeys(loaded);
                setEditingKey(null);
            } else {
                setSaveStatus('error');
            }
        } catch {
            setSaveStatus('error');
        }
        setSaving(false);
        setTimeout(() => setSaveStatus('idle'), 5000);
    }, [apiKeys, apiBases, wsRef, browserResearch, smokeEnabled, browserHeadful, forceVisibleReview, maxRetries, comfyUiUrl, comfyWorkflowTemplate, imgProvider, imgApiKey, imgBaseUrl, imgDefaultModel, imgDefaultSize, imgMaxImages, imgAutoCrop, analystPreferredSites, analystCrawlIntensity, analystUseScrapling, analystEnableQuerySearch, nodeModelPreferences, thinkingDepth, reviewerMaxRejections, walkthroughLang, peerBuildersShareModelWhenMultikey, lang]);

    // Handle key input change with sanitization
    const handleKeyChange = useCallback((key: keyof ApiKeys, value: string) => {
        const sanitized = sanitizeInput(value);
        setApiKeys(prev => ({ ...prev, [key]: sanitized }));
    }, []);

    // Prevent key display via browser DevTools / autocomplete
    const keyInputProps = {
        autoComplete: 'off' as const,
        'data-lpignore': 'true', // LastPass ignore
        'data-1p-ignore': 'true', // 1Password ignore
        spellCheck: false as const,
    };

    const modelOptions = (modelCatalog.length > 0 ? modelCatalog : FALLBACK_MODEL_CATALOG)
        .slice()
        .sort((a, b) => {
            const providerCompare = a.provider.localeCompare(b.provider);
            return providerCompare !== 0 ? providerCompare : a.id.localeCompare(b.id);
        });
    const selectedRelayTemplate = relayCatalog.find((item) => item.id === relayDraft.templateId);

    const updateNodeModelPreference = useCallback((role: string, slotIndex: number, nextModel: string) => {
        setNodeModelPreferences((prev) => {
            const next = { ...prev };
            const current = [...(next[role] || [])];
            current[slotIndex] = nextModel;
            const normalized = current
                .map((item) => String(item || '').trim())
                .filter(Boolean)
                .filter((item, index, arr) => arr.indexOf(item) === index)
                .slice(0, 6);
            if (normalized.length > 0) {
                next[role] = normalized;
            } else {
                delete next[role];
            }
            return next;
        });
    }, []);

    if (!open) return null;

    const t = (en: string, zh: string) => lang === 'zh' ? zh : en;

    return (
        <div className="modal-overlay" onClick={handleClose}>
            <div className="modal-container" onClick={e => e.stopPropagation()}>
                {/* Header */}
                <div className="modal-header">
                    <h3>{t('Settings', '设置')}</h3>
                    <button className="modal-close" onClick={handleClose}>✕</button>
                </div>

                {/* Tabs */}
                <div className="settings-tabs">
                    {TABS.map(tb => (
                        <button
                            key={tb.id}
                            className={`settings-tab ${tab === tb.id ? 'active' : ''}`}
                            onClick={() => setTab(tb.id)}
                        >
                            {lang === 'zh' ? tb.label_zh : tb.label_en}
                        </button>
                    ))}
                </div>

                {/* Body */}
                <div className="modal-body">
                    {tab === 'conn' && (
                        <>
                            {/* Connection Status */}
                            <div className="s-section">
                                <div className="s-section-title">{t('Connection Status', '连接状态')}</div>
                                <div className="s-row">
                                    <span className={`s-dot ${connected ? 'on' : 'off'}`} />
                                    <span className={`s-badge ${connected ? 'green' : 'red'}`}>
                                        {connected ? t('Connected', '已连接') : t('Disconnected', '未连接')}
                                    </span>
                                </div>
                                <div className="s-row">
                                    <label>WebSocket URL</label>
                                    <input
                                        className="s-input"
                                        value={wsUrl}
                                        onChange={e => onWsUrlChange(e.target.value)}
                                    />
                                </div>
                            </div>

                            {/* API Keys */}
                            <div className="s-section">
                                <div className="s-section-title">{t('API Keys', 'API 密钥')}</div>
                                <div className="s-hint" style={{ marginBottom: 8 }}>
                                    {t(
                                        'Keys are sent directly to your local backend. Never shared with third parties. For relay/proxy APIs, fill in the Base URL below each key.',
                                        '密钥仅发送到本地后端，绝不会发送给第三方。如使用中转 API，请在每个密钥下方填写中转地址（Base URL）。'
                                    )}
                                </div>
                                {(['openai', 'claude', 'gemini', 'kimi', 'deepseek', 'qwen', 'minimax', 'zhipu', 'doubao', 'yi', 'aigate'] as const).map(k => {
                                    const isEditing = editingKey === k;
                                    const hasValue = !!apiKeys[k];
                                    const isConfigured = loadedKeys.has(k);
                                    const displayValue = isEditing ? apiKeys[k] : (hasValue ? maskKey(apiKeys[k]) : '');
                                    // v5.8.6: optional secondary key for concurrent-node load balancing
                                    const secondaryKeyName = `${k}_2` as keyof ApiKeys;
                                    const editingSecondary = editingKey === (secondaryKeyName as string);
                                    const hasSecondary = !!apiKeys[secondaryKeyName];
                                    const secondaryDisplay = editingSecondary
                                        ? (apiKeys[secondaryKeyName] as string || '')
                                        : (hasSecondary ? maskKey(apiKeys[secondaryKeyName] as string) : '');
                                    const displayLabel: Record<string, string> = {
                                        openai: 'OpenAI',
                                        claude: 'Claude',
                                        gemini: 'Gemini',
                                        kimi: 'Kimi',
                                        deepseek: 'DeepSeek',
                                        qwen: 'Qwen (通义千问)',
                                        minimax: 'MiniMax (M2.x)',
                                        zhipu: 'Zhipu / GLM',
                                        doubao: 'Doubao (豆包)',
                                        yi: 'Yi / 01.AI',
                                        aigate: 'AiGate (private-relay 中转)',
                                    };
                                    return (
                                        <div key={k} style={{ marginBottom: 6 }}>
                                            <div className="s-row">
                                                <label>{displayLabel[k] || (k.charAt(0).toUpperCase() + k.slice(1))}</label>
                                                <input
                                                    className="s-input"
                                                    type={isEditing ? 'text' : 'password'}
                                                    value={displayValue}
                                                    onChange={e => handleKeyChange(k, e.target.value)}
                                                    onFocus={() => setEditingKey(k)}
                                                    onBlur={() => setEditingKey(null)}
                                                    placeholder={isConfigured && !hasValue ? '••• (已配置)' : 'sk-...'}
                                                    {...keyInputProps}
                                                />
                                                {(hasValue || isConfigured) && (
                                                    <span style={{ fontSize: 8, color: 'var(--green)', fontWeight: 600 }}>
                                                        {hasValue ? '✓ 新' : '✓'}
                                                    </span>
                                                )}
                                            </div>
                                            <div className="s-row" style={{ paddingLeft: 16, opacity: 0.85, marginTop: 2 }}>
                                                <label style={{ fontSize: 9, color: 'var(--text3)' }}>
                                                    {t('Backup Key (optional, for concurrent speed)', '备用 Key（可选，加速并发）')}
                                                </label>
                                                <input
                                                    className="s-input"
                                                    type={editingSecondary ? 'text' : 'password'}
                                                    value={secondaryDisplay}
                                                    onChange={e => {
                                                        const cleaned = sanitizeInput(e.target.value);
                                                        setApiKeys(prev => ({ ...prev, [secondaryKeyName]: cleaned }));
                                                    }}
                                                    onFocus={() => setEditingKey(secondaryKeyName as string)}
                                                    onBlur={() => setEditingKey(null)}
                                                    placeholder={t('Leave empty if you only have one key', '只有一个 Key 可留空')}
                                                    style={{ fontSize: 10 }}
                                                    {...keyInputProps}
                                                />
                                                {hasSecondary && (
                                                    <span style={{ fontSize: 8, color: 'var(--green)', fontWeight: 600 }}>✓</span>
                                                )}
                                            </div>
                                            <div className="s-row" style={{ paddingLeft: 16, opacity: 0.85, marginTop: 2 }}>
                                                <label style={{ fontSize: 9, color: 'var(--text3)' }}>
                                                    {t('Base URL', '中转地址')}
                                                </label>
                                                <input
                                                    className="s-input"
                                                    value={apiBases[k] || ''}
                                                    onChange={e => setApiBases(prev => ({ ...prev, [k]: sanitizeInput(e.target.value) }))}
                                                    placeholder={t('Leave empty for official endpoint', '留空使用官方地址')}
                                                    style={{ fontSize: 10 }}
                                                />
                                            </div>
                                        </div>
                                    );
                                })}
                                <div className="flex items-center gap-2 mt-2">
                                    <button className="btn btn-primary text-[10px]" onClick={saveToBackend} disabled={saving}>
                                        {saving ? t('Saving...', '保存中...') : t('Save to Backend', '保存到后端')}
                                    </button>
                                    {saveStatus === 'success' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                    {saveStatus === 'error' && <span className="text-[9px]" style={{ color: 'var(--red)' }}>✗ {t('Failed', '保存失败')}</span>}
                                </div>

                                {/* Available models after save */}
                                {Object.keys(availableModels).length > 0 && (
                                    <div className="s-section" style={{ marginTop: 12, padding: '8px 10px', background: 'rgba(91,140,255,0.05)', borderRadius: 8, border: '1px solid rgba(91,140,255,0.15)' }}>
                                        <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--blue)', marginBottom: 6 }}>
                                            {t('Available Models', '可用模型')}
                                        </div>
                                        {Object.entries(availableModels).map(([provider, models]) => {
                                            const providerNames: Record<string, string> = {
                                                openai: 'OpenAI', anthropic: 'Claude', google: 'Gemini',
                                                deepseek: 'DeepSeek', kimi: 'Kimi', qwen: '通义千问', ollama: 'Ollama',
                                                minimax: 'MiniMax', zhipu: 'Zhipu/GLM', doubao: 'Doubao', yi: 'Yi/01.AI',
                                            };
                                            return (
                                                <div key={provider} style={{ marginBottom: 4 }}>
                                                    <span style={{ fontSize: 9, color: 'var(--green)', fontWeight: 600 }}>
                                                        {providerNames[provider] || provider}:
                                                    </span>
                                                    <span style={{ fontSize: 9, color: 'var(--text2)', marginLeft: 4 }}>
                                                        {(models as string[]).join(', ')}
                                                    </span>
                                                </div>
                                            );
                                        })}
                                        <div style={{ fontSize: 8, color: 'var(--text3)', marginTop: 4 }}>
                                            {t('You can select these models on each node', '你可以在每个节点上选择这些模型')}
                                        </div>
                                    </div>
                                )}
                            </div>

                            <div className="s-section">
                                <div className="s-section-title">{t('Relay Pool', '中转池')}</div>
                                <div className="s-hint" style={{ marginBottom: 8 }}>
                                    {t(
                                        'A single Base URL above is only a direct route. To make GPT-5.4 or other relay-backed models truly fail over smoothly, add relay endpoints here. Relay API keys may be left empty to reuse the saved provider key when available.',
                                        '上面的单个 Base URL 只是直连路由。要让 GPT-5.4 或其他中转模型真正走中转池并具备切换能力，需要在这里添加 relay endpoint。Relay 的 API key 留空时，会优先复用已保存的对应 provider key。'
                                    )}
                                </div>
                                <div className="s-row">
                                    <label>{t('Template', '模板')}</label>
                                    <select
                                        className="s-input"
                                        value={relayDraft.templateId}
                                        onChange={(e) => applyRelayTemplateDraft(e.target.value)}
                                    >
                                        {relayCatalog.map((item) => (
                                            <option key={item.id} value={item.id}>
                                                {item.label}
                                            </option>
                                        ))}
                                    </select>
                                </div>
                                {selectedRelayTemplate?.description && (
                                    <div className="s-hint" style={{ marginBottom: 8 }}>
                                        {selectedRelayTemplate.description}
                                    </div>
                                )}
                                <div className="s-row">
                                    <label>{t('Name', '名称')}</label>
                                    <input
                                        className="s-input"
                                        value={relayDraft.name}
                                        onChange={(e) => updateRelayDraft({ name: sanitizeInput(e.target.value) })}
                                        placeholder={selectedRelayTemplate?.label || 'Relay Endpoint'}
                                    />
                                </div>
                                <div className="s-row">
                                    <label>{t('Base URL', '中转地址')}</label>
                                    <input
                                        className="s-input"
                                        value={relayDraft.baseUrl}
                                        onChange={(e) => updateRelayDraft({ baseUrl: sanitizeInput(e.target.value) })}
                                        placeholder={selectedRelayTemplate?.default_base_url || 'https://.../v1'}
                                    />
                                </div>
                                <div className="flex items-center gap-2" style={{ marginBottom: 8 }}>
                                    <button
                                        className="btn text-[10px]"
                                        onClick={importOpenAiBaseIntoRelay}
                                        type="button"
                                    >
                                        {t('Import OpenAI Base', '导入 OpenAI Base')}
                                    </button>
                                    <span className="text-[9px]" style={{ color: 'var(--text3)' }}>
                                        {apiBases.openai ? apiBases.openai : t('No OpenAI Base configured', '当前未配置 OpenAI Base')}
                                    </span>
                                </div>
                                <div className="s-row">
                                    <label>{t('Relay Key', '中转密钥')}</label>
                                    <input
                                        className="s-input"
                                        type="password"
                                        value={relayDraft.apiKey}
                                        onChange={(e) => updateRelayDraft({ apiKey: sanitizeInput(e.target.value) })}
                                        placeholder={t('Optional: reuse saved provider key', '可选：留空则复用已保存 provider key')}
                                        autoComplete="off"
                                    />
                                </div>
                                <div className="s-row">
                                    <label>{t('Models', '模型')}</label>
                                    <input
                                        className="s-input"
                                        value={relayDraft.models}
                                        onChange={(e) => updateRelayDraft({ models: sanitizeInput(e.target.value) })}
                                        placeholder={(selectedRelayTemplate?.default_models || []).join(', ') || 'gpt-5.4, gpt-4o'}
                                    />
                                </div>
                                <div className="flex items-center gap-2 mt-2">
                                    <button className="btn btn-primary text-[10px]" onClick={addRelayEndpoint} disabled={relaySaving}>
                                        {relaySaving ? t('Adding...', '添加中...') : t('Add Relay', '添加中转')}
                                    </button>
                                    {relayStatus === 'ok:add' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Relay added', '中转已添加')}</span>}
                                    {relayStatus === 'ok:test' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Relay test passed', '中转测试通过')}</span>}
                                    {relayStatus === 'ok:remove' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Relay removed', '中转已删除')}</span>}
                                    {relayStatus && !relayStatus.startsWith('ok:') && (
                                        <span className="text-[9px]" style={{ color: 'var(--red)' }}>
                                            {relayStatus}
                                        </span>
                                    )}
                                </div>

                                <div style={{ marginTop: 12, display: 'grid', gap: 8 }}>
                                    {relayEndpoints.length === 0 && (
                                        <div className="s-hint">
                                            {t(
                                                'No relay endpoints configured. This is why GPT-5.4 is still going straight to the current OpenAI-compatible gateway instead of using a relay pool.',
                                                '当前还没有配置任何 relay endpoint，这就是为什么 GPT-5.4 仍然直接走当前 OpenAI-compatible 网关，而不是走中转池。'
                                            )}
                                        </div>
                                    )}
                                    {relayEndpoints.map((relay) => {
                                        const lastTestOk = Boolean(relay.last_test?.success || relay.last_test?.connectivity_ok);
                                        const lastTestLabel = relay.last_test
                                            ? (lastTestOk
                                                ? `${t('Test OK', '测试通过')}${relay.last_test?.latency_ms ? ` · ${relay.last_test.latency_ms}ms` : ''}`
                                                : t('Test Failed', '测试失败'))
                                            : t('Untested', '未测试');
                                        return (
                                            <div
                                                key={relay.id}
                                                style={{
                                                    padding: '10px 12px',
                                                    borderRadius: 10,
                                                    border: '1px solid rgba(148,163,184,0.18)',
                                                    background: 'rgba(15,23,42,0.16)',
                                                }}
                                            >
                                                <div className="flex items-center justify-between gap-3">
                                                    <div>
                                                        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text1)' }}>
                                                            {relay.name || relay.id}
                                                        </div>
                                                        <div style={{ fontSize: 9, color: 'var(--text3)', marginTop: 2 }}>
                                                            {PROVIDER_LABELS[relay.provider] || relay.provider || 'Relay'} · {relay.base_url || '-'}
                                                        </div>
                                                        <div style={{ fontSize: 9, color: 'var(--text2)', marginTop: 4 }}>
                                                            {(relay.models || []).join(', ') || t('Template defaults', '模板默认模型')}
                                                        </div>
                                                    </div>
                                                    <div className="flex items-center gap-2">
                                                        <span
                                                            style={{
                                                                fontSize: 9,
                                                                padding: '3px 8px',
                                                                borderRadius: 999,
                                                                color: lastTestOk ? 'var(--green)' : 'var(--text3)',
                                                                border: `1px solid ${lastTestOk ? 'rgba(34,197,94,0.35)' : 'rgba(148,163,184,0.18)'}`,
                                                                background: lastTestOk ? 'rgba(34,197,94,0.08)' : 'rgba(148,163,184,0.06)',
                                                            }}
                                                        >
                                                            {lastTestLabel}
                                                        </span>
                                                        <button
                                                            className="btn text-[10px]"
                                                            onClick={() => testRelayEndpoint(relay.id)}
                                                            disabled={relayActionId === relay.id}
                                                            type="button"
                                                        >
                                                            {relayActionId === relay.id ? t('Testing...', '测试中...') : t('Test', '测试')}
                                                        </button>
                                                        <button
                                                            className="btn text-[10px]"
                                                            onClick={() => removeRelayEndpoint(relay.id)}
                                                            disabled={relayActionId === relay.id}
                                                            type="button"
                                                        >
                                                            {t('Delete', '删除')}
                                                        </button>
                                                    </div>
                                                </div>
                                                {relay.last_test?.error && (
                                                    <div style={{ fontSize: 9, color: 'var(--red)', marginTop: 6 }}>
                                                        {relay.last_test.error}
                                                    </div>
                                                )}
                                            </div>
                                        );
                                    })}
                                </div>
                            </div>
                        </>
                    )}

                    {tab === 'perm' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">{t('Security Config', '安全配置')}</div>
                                <div className="s-row">
                                    <label>L4 {t('Password', '密码')}</label>
                                    <input className="s-input" type="password" value={l4Pass} onChange={e => setL4Pass(e.target.value)} autoComplete="off" />
                                </div>
                                <div className="s-toggle">
                                    <label>{t('Auto-approve L2 ops', '自动批准L2操作')}</label>
                                    <input type="checkbox" checked={autoL2} onChange={e => setAutoL2(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    L1: {t('Read-only', '只读')} · L2: {t('File/Network', '文件/网络')} · L3: {t('Confirm required', '需确认')} · L4: {t('Password required', '需密码')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Execution Whitelist', '执行白名单')}</div>
                                <div className="s-row">
                                    <label>{t('Allowed Dirs', '允许目录')}</label>
                                    <input className="s-input" value={allowedDirs} onChange={e => setAllowedDirs(e.target.value)} />
                                </div>
                                <div className="s-row">
                                    <label>{t('Max File Size', '最大文件')}</label>
                                    <input className="s-input" value={maxFileSize} onChange={e => setMaxFileSize(e.target.value)} style={{ width: 80, flex: 'none' }} />
                                </div>
                            </div>
                        </>
                    )}

                    {tab === 'quality' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">{t('Visual Verification', '视觉验收')}</div>
                                <div className="s-toggle">
                                    <label>{t('Playwright Smoke Test', 'Playwright 烟雾测试')}</label>
                                    <input type="checkbox" checked={smokeEnabled} onChange={e => setSmokeEnabled(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {t('Run headless browser test to validate rendered page', '运行无头浏览器测试验证页面渲染效果')}
                                </div>
                                <div className="s-toggle" style={{ marginTop: 8 }}>
                                    <label>{t('Visible Browser Window (Headful)', '可见浏览器窗口 (Headful)')}</label>
                                    <input type="checkbox" checked={browserHeadful} onChange={e => setBrowserHeadful(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {browserHeadful
                                        ? t('Enabled: reviewer/tester browser actions open a visible Chromium window (slower but observable).', '已开启：审查员/测试员浏览器动作会打开可见 Chromium 窗口（更慢但可观察）。')
                                        : t('Disabled: browser actions run in headless mode (faster, silent).', '已关闭：浏览器动作使用无头模式（更快、静默）。')}
                                </div>
                                <div className="s-toggle" style={{ marginTop: 8 }}>
                                    <label>{t('Force Visible Browser for Reviewer/Tester', '审查员/测试员强制可见浏览器')}</label>
                                    <input type="checkbox" checked={forceVisibleReview} onChange={e => setForceVisibleReview(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {forceVisibleReview
                                        ? t('Enabled: reviewer/tester stages force browser headful mode even if global mode is headless.', '已开启：即使全局是无头模式，审查员/测试员阶段也会强制可见窗口。')
                                        : t('Disabled: reviewer/tester follow global headful/headless mode.', '已关闭：审查员/测试员跟随全局浏览器模式。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Retry Strategy', '重试策略')}</div>
                                <div className="s-row">
                                    <label>{t('Max Retries', '最大重试')}</label>
                                    <select className="s-input" value={maxRetries} onChange={e => setMaxRetries(Number(e.target.value))} style={{ width: 'auto' }}>
                                        <option value={1}>1</option>
                                        <option value={2}>2</option>
                                        <option value={3}>3 ({t('default', '默认')})</option>
                                        <option value={5}>5</option>
                                    </select>
                                </div>
                                <div className="s-hint">
                                    {t('Failures auto-downgrade model. If a node has its own chain in "Node Models", that chain takes priority.',
                                       '失败时会自动降级模型；如果你在“节点模型”里给某个节点单独配置了链路，则优先按那个链路执行。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Builder Enhancement', 'Builder 增强')}</div>
                                <div className="s-toggle">
                                    <label>{t('Web Research Mode', '联网参考模式')}</label>
                                    <input type="checkbox" checked={browserResearch} onChange={e => setBrowserResearch(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {t('Allow builder to browse 1-2 reference pages for design inspiration',
                                       '允许 builder 浏览 1-2 个参考页面获取设计灵感')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Analyst Research Pipeline', '分析师研究管线')}</div>
                                <div className="s-row">
                                    <label>{t('Crawl Intensity', '爬取强度')}</label>
                                    <select
                                        className="s-input"
                                        value={analystCrawlIntensity}
                                        onChange={e => setAnalystCrawlIntensity(e.target.value as AnalystCrawlIntensity)}
                                        style={{ width: 'auto' }}
                                    >
                                        <option value="off">{t('Off', '关闭')}</option>
                                        <option value="low">{t('Low', '少量')}</option>
                                        <option value="medium">{t('Medium', '中等')}</option>
                                        <option value="high">{t('High', '大量')}</option>
                                    </select>
                                </div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {t(
                                        'Controls how aggressively the analyst searches GitHub/docs/source references before handing builders a brief.',
                                        '控制分析师在给 builder 下发 brief 之前，搜索 GitHub / 文档 / 源码参考的积极程度。'
                                    )}
                                </div>
                                <div className="s-toggle" style={{ marginTop: 8 }}>
                                    <label>{t('Enable Query Search', '启用搜索查询')}</label>
                                    <input type="checkbox" checked={analystEnableQuerySearch} onChange={e => setAnalystEnableQuerySearch(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {t(
                                        'Allows analyst to use query-based search before batch-fetching concrete URLs.',
                                        '允许分析师先做 query 搜索，再批量抓取具体 URL。'
                                    )}
                                </div>
                                <div className="s-toggle" style={{ marginTop: 8 }}>
                                    <label>{t('Prefer Scrapling', '优先使用 Scrapling')}</label>
                                    <input type="checkbox" checked={analystUseScrapling} onChange={e => setAnalystUseScrapling(e.target.checked)} />
                                </div>
                                <div className="s-hint">
                                    {t(
                                        'When available, use Scrapling before Crawl4AI/urllib for harder sites and richer extraction.',
                                        '可用时优先使用 Scrapling，再回退到 Crawl4AI/urllib，以提升复杂页面抓取与抽取质量。'
                                    )}
                                </div>
                                <div className="s-row" style={{ alignItems: 'flex-start', marginTop: 8 }}>
                                    <label style={{ paddingTop: 6 }}>{t('Preferred Sites', '优先站点')}</label>
                                    <textarea
                                        className="s-input"
                                        value={analystPreferredSites}
                                        onChange={e => setAnalystPreferredSites(sanitizeMultilineInput(e.target.value))}
                                        placeholder={'https://github.com\nhttps://threejs.org\nhttps://developer.mozilla.org'}
                                        rows={6}
                                        style={{ minHeight: 108, resize: 'vertical', lineHeight: 1.5 }}
                                    />
                                </div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {t(
                                        'One site per line. The analyst will prioritize these domains for source_fetch query/search and source allocation to builders.',
                                        '每行一个站点。分析师会优先在这些域名内进行 source_fetch 搜索，并把抓到的源码/文档优先分配给各个 builder。'
                                    )}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Image Generation (direct API) — v6.2', '图像生成（直连 API）— v6.2')}</div>
                                <div className="s-row">
                                    <label>{t('Provider', '服务商')}</label>
                                    <select
                                        className="s-input"
                                        value={imgProvider}
                                        onChange={e => {
                                            const v = e.target.value as typeof imgProvider;
                                            setImgProvider(v);
                                            // Sensible default model per provider
                                            if (v === 'doubao-image' || v === 'seedream') {
                                                if (!imgDefaultModel) setImgDefaultModel('doubao-seedream-4-0-250828');
                                            } else if (v === 'tongyi') {
                                                if (!imgDefaultModel) setImgDefaultModel('wanx2.1-t2i-turbo');
                                            } else if (v === 'flux-fal') {
                                                if (!imgDefaultModel) setImgDefaultModel('fal-ai/flux/schnell');
                                            } else if (v === 'openai-compat') {
                                                if (!imgDefaultModel) setImgDefaultModel('dall-e-3');
                                            }
                                        }}
                                    >
                                        <option value="">{t('— none (disabled) —', '— 未启用 —')}</option>
                                        <option value="doubao-image">{t('Doubao / Seedream (Volcengine, fastest)', '豆包 / Seedream（火山引擎，推荐最快）')}</option>
                                        <option value="tongyi">{t('Tongyi WanX (Alibaba DashScope)', '通义万相（阿里 DashScope）')}</option>
                                        <option value="flux-fal">{t('FLUX schnell (fal.ai, 1.5s)', 'FLUX schnell（fal.ai，1.5 秒）')}</option>
                                        <option value="openai-compat">{t('OpenAI-compatible (relay / DALL-E-3)', 'OpenAI 兼容（中转站 / DALL-E-3）')}</option>
                                    </select>
                                </div>
                                {imgProvider && (
                                    <>
                                        <div className="s-row">
                                            <label>API Key</label>
                                            <input
                                                className="s-input"
                                                type="password"
                                                value={imgApiKey}
                                                onChange={e => setImgApiKey(e.target.value.trim())}
                                                placeholder={imgProvider === 'doubao-image' || imgProvider === 'seedream' ? 'Volcengine Ark API Key'
                                                    : imgProvider === 'tongyi' ? 'sk-xxx (DashScope)'
                                                        : imgProvider === 'flux-fal' ? 'fal.ai Key'
                                                            : 'sk-xxx'}
                                            />
                                        </div>
                                        {(imgProvider === 'openai-compat' || imgProvider === 'tongyi' || imgProvider === 'doubao-image' || imgProvider === 'seedream') && (
                                            <div className="s-row">
                                                <label>Base URL</label>
                                                <input
                                                    className="s-input"
                                                    value={imgBaseUrl}
                                                    onChange={e => setImgBaseUrl(sanitizeInput(e.target.value))}
                                                    placeholder={imgProvider === 'openai-compat' ? 'https://your-relay/v1'
                                                        : imgProvider === 'tongyi' ? 'https://dashscope.aliyuncs.com'
                                                            : 'https://ark.cn-beijing.volces.com/api/v3'}
                                                />
                                            </div>
                                        )}
                                        <div className="s-row">
                                            <label>{t('Model', '模型')}</label>
                                            <input
                                                className="s-input"
                                                value={imgDefaultModel}
                                                onChange={e => setImgDefaultModel(sanitizeInput(e.target.value))}
                                                placeholder={imgProvider === 'doubao-image' || imgProvider === 'seedream' ? 'doubao-seedream-4-0-250828'
                                                    : imgProvider === 'tongyi' ? 'wanx2.1-t2i-turbo'
                                                        : imgProvider === 'flux-fal' ? 'fal-ai/flux/schnell'
                                                            : 'dall-e-3'}
                                            />
                                        </div>
                                        <div className="s-row">
                                            <label>{t('Default Size', '默认尺寸')}</label>
                                            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                                                {[
                                                    { v: '1024x1024', label: '1:1 sprite' },
                                                    { v: '1920x1080', label: '16:9 hero' },
                                                    { v: '1280x720', label: '16:9 PPT' },
                                                    { v: '1536x1024', label: '3:2 banner' },
                                                ].map(chip => (
                                                    <button
                                                        key={chip.v}
                                                        type="button"
                                                        onClick={() => setImgDefaultSize(chip.v)}
                                                        style={{
                                                            padding: '4px 10px',
                                                            borderRadius: 6,
                                                            border: imgDefaultSize === chip.v ? '1px solid #a855f7' : '1px solid rgba(255,255,255,0.1)',
                                                            background: imgDefaultSize === chip.v ? 'rgba(168,85,247,0.15)' : 'transparent',
                                                            color: imgDefaultSize === chip.v ? '#d4a8ff' : 'var(--text2)',
                                                            fontSize: 11,
                                                            cursor: 'pointer',
                                                        }}
                                                    >
                                                        {chip.v} · {chip.label}
                                                    </button>
                                                ))}
                                                <input
                                                    className="s-input"
                                                    value={imgDefaultSize}
                                                    onChange={e => setImgDefaultSize(sanitizeInput(e.target.value))}
                                                    placeholder="custom WxH"
                                                    style={{ maxWidth: 140 }}
                                                />
                                            </div>
                                        </div>
                                        <div className="s-row">
                                            <label>{t('Max Images / Run', '单次上限')}</label>
                                            <input
                                                className="s-input"
                                                type="number"
                                                min={1}
                                                max={40}
                                                value={imgMaxImages}
                                                onChange={e => setImgMaxImages(Math.max(1, Math.min(40, Number(e.target.value) || 10)))}
                                                style={{ maxWidth: 100 }}
                                            />
                                        </div>
                                        <div className="s-toggle" style={{ marginTop: 6 }}>
                                            <label>{t('Auto-crop to 16:9 / 4:3 / 1:1 / 3:4', '自动裁剪为 16:9 / 4:3 / 1:1 / 3:4')}</label>
                                            <input type="checkbox" checked={imgAutoCrop} onChange={e => setImgAutoCrop(e.target.checked)} />
                                        </div>
                                        <div className="s-row" style={{ marginTop: 8 }}>
                                            <label>{t('Connectivity Test', '连通测试')}</label>
                                            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                                                <button
                                                    type="button"
                                                    className="btn-ghost"
                                                    disabled={imgTestLoading || !imgProvider || !imgApiKey}
                                                    onClick={async () => {
                                                        setImgTestLoading(true);
                                                        setImgTestResult(null);
                                                        try {
                                                            const resp = await fetch(`${API_BASE}/api/settings/image_gen/test`, {
                                                                method: 'POST',
                                                                headers: { 'Content-Type': 'application/json' },
                                                                body: JSON.stringify({
                                                                    provider: imgProvider,
                                                                    api_key: imgApiKey,
                                                                    base_url: imgBaseUrl,
                                                                    default_model: imgDefaultModel,
                                                                    default_size: imgDefaultSize,
                                                                }),
                                                            });
                                                            const result = await resp.json();
                                                            setImgTestResult(result);
                                                        } catch (e) {
                                                            setImgTestResult({ ok: false, error: String(e) });
                                                        } finally {
                                                            setImgTestLoading(false);
                                                        }
                                                    }}
                                                    style={{ padding: '6px 14px', fontSize: 12 }}
                                                >
                                                    {imgTestLoading ? t('Testing…', '测试中…') : t('🧪 Test & Preview', '🧪 测试并预览')}
                                                </button>
                                                {imgTestResult && imgTestResult.ok && imgTestResult.image_base64 && (
                                                    <>
                                                        <img
                                                            src={imgTestResult.image_base64}
                                                            alt="test preview"
                                                            style={{ width: 64, height: 64, borderRadius: 6, border: '1px solid rgba(255,255,255,0.1)', objectFit: 'cover' }}
                                                        />
                                                        <span style={{ color: '#22c55e', fontSize: 11 }}>
                                                            ✓ {imgTestResult.latency_ms}ms
                                                        </span>
                                                    </>
                                                )}
                                                {imgTestResult && !imgTestResult.ok && (
                                                    <span style={{ color: '#ef4444', fontSize: 11 }}>
                                                        ✗ {imgTestResult.error}
                                                    </span>
                                                )}
                                            </div>
                                        </div>
                                    </>
                                )}
                                <div className="s-hint" style={{ lineHeight: 1.7, marginTop: 10 }}>
                                    {imgProvider
                                        ? t('Direct provider active — the imagegen node will call this API. Legacy ComfyUI fields below are ignored.',
                                            '直连 provider 已启用 — imagegen 节点会调此 API，下方 ComfyUI 字段会被忽略。')
                                        : t('Select a provider above to enable real image generation. Leave empty to use ComfyUI (if configured) or SVG placeholders.',
                                            '选择一个 provider 以启用真实图片生成。留空则使用 ComfyUI（若已配置）或 SVG 占位图。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('ComfyUI (legacy)', 'ComfyUI（旧路径）')}</div>
                                <div className="s-row">
                                    <label>ComfyUI URL</label>
                                    <input
                                        className="s-input"
                                        value={comfyUiUrl}
                                        onChange={e => setComfyUiUrl(sanitizeInput(e.target.value))}
                                        placeholder="http://127.0.0.1:8188"
                                    />
                                </div>
                                <div className="s-row">
                                    <label>{t('Workflow Template', '工作流模板')}</label>
                                    <input
                                        className="s-input"
                                        value={comfyWorkflowTemplate}
                                        onChange={e => setComfyWorkflowTemplate(sanitizeInput(e.target.value))}
                                        placeholder="/path/to/workflow.json"
                                    />
                                </div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {imageBackendAvailable
                                        ? t('Configured: planner may auto-insert imagegen / spritesheet / assetimport for asset-heavy goals.',
                                            '已配置：规划器在素材密集型任务里会自动插入 imagegen / spritesheet / assetimport。')
                                        : t('Not configured: image nodes will not be auto-inserted. The system will fall back to prompt packs and placeholder assets.',
                                            '未配置：系统不会自动插入图像节点，会退回到提示词包和占位素材方案。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Model Strategy', '模型策略')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.6 }}>
                                    Simple → {t('fastest model (kimi/deepseek)', '最快模型 (kimi/deepseek)')}<br/>
                                    Standard → {t('default model', '默认模型')}<br/>
                                    Pro → {t('strongest model (gpt-5.4/claude-4)', '最强模型 (gpt-5.4/claude-4)')}<br/>
                                    <span style={{ fontSize: 8, color: 'var(--text3)' }}>
                                        {t('Auto-selects based on configured API keys', '根据已配置的 API 密钥自动选择')}
                                    </span>
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('What these settings mean', '这些设置具体是什么意思')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    • {t('Smoke Test: visual browser check in tester stage. Off = faster, On = stricter page validation.',
                                           'Smoke Test：Tester 阶段做真实浏览器可视化检查。关闭更快，开启更严格。')}<br/>
                                    • {t('Max Retries: max retry count after a subtask fails. Higher = higher success chance, but slower.',
                                           'Max Retries：子任务失败后的最大重试次数。值越大成功率通常更高，但更慢。')}<br/>
                                    • {t('Web Research Mode: allows Builder to browse references for better UI inspiration.',
                                           '联网参考模式：允许 Builder 浏览参考页面，提升 UI 设计灵感。')}<br/>
                                    • {t('Model Strategy: Simple for speed, Standard for balance, Pro for best quality.',
                                           '模型策略：Simple 主打速度，Standard 平衡，Pro 追求最高质量。')}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Browser Engine Permissions', '浏览器引擎权限')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    <b>{t('Headless mode (default)', 'Headless 模式（默认）')}</b><br/>
                                    • {t('No macOS permissions required', '不需要任何 macOS 系统权限')}<br/>
                                    • {t('Runs silently in background, takes screenshots automatically', '后台静默运行，自动截图分析')}<br/>
                                    • {t('Requires:', '需要安装：')} <code>python3 -m playwright install chromium</code><br/><br/>
                                    <b>{t('Headful mode (visible browser)', 'Headful 模式（可见浏览器）')}</b><br/>
                                    • {t('For Playwright page automation, macOS Accessibility/Screen Recording is usually NOT required', '仅 Playwright 页面自动化时，通常不需要 macOS 辅助功能/屏幕录制权限')}<br/>
                                    • {t('Desktop-control of other apps (not this browser test) may require those permissions', '如果是控制其他桌面应用（非本浏览器测试），才可能需要这些权限')}
                                </div>
                            </div>
                            <div className="flex items-center gap-2 mt-2">
                                <button className="btn btn-primary text-[10px]" onClick={saveToBackend} disabled={saving}>
                                    {saving ? t('Saving...', '保存中...') : t('Save Quality Settings', '保存质量设置')}
                                </button>
                                {saveStatus === 'success' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                {saveStatus === 'error' && <span className="text-[9px]" style={{ color: 'var(--red)' }}>✗ {t('Failed', '保存失败')}</span>}
                            </div>
                        </>
                    )}

                    {tab === 'nodes' && (
                        <>
                            <div className="s-section" style={{
                                background: thinkingDepth === 'fast'
                                    ? 'rgba(250, 204, 21, 0.08)'
                                    : 'rgba(59, 130, 246, 0.08)',
                                border: `1px solid ${thinkingDepth === 'fast' ? 'rgba(250, 204, 21, 0.25)' : 'rgba(59, 130, 246, 0.25)'}`,
                                borderRadius: 8,
                                padding: '12px 14px',
                            }}>
                                <div className="s-section-title" style={{ fontSize: '12px' }}>
                                    {thinkingDepth === 'fast' ? '\u26A1' : '\u{1F9E0}'} {t('Speed Mode (Fast / Deep)', '速度模式 (Fast / Deep)')}
                                </div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {t(
                                        'Controls the speed/quality tradeoff for ALL AI nodes. Fast = lower reasoning effort + tighter timeouts (faster but less thorough). Deep = full reasoning power + relaxed timeouts (slower but higher quality).',
                                        '控制所有 AI 节点的速度/质量权衡。Fast = 较低推理力度 + 紧凑超时（更快但不够深入）。Deep = 完整推理能力 + 宽松超时（更慢但质量更高）。'
                                    )}
                                </div>
                                <div className="s-row" style={{ marginTop: 8 }}>
                                    <label style={{ fontWeight: 600 }}>{t('Global Mode', '全局模式')}</label>
                                    <select
                                        className="s-input"
                                        value={thinkingDepth}
                                        onChange={(e) => setThinkingDepth(e.target.value as 'fast' | 'deep')}
                                        style={{
                                            fontWeight: 600,
                                            color: thinkingDepth === 'fast' ? '#facc15' : '#60a5fa',
                                        }}
                                    >
                                        <option value="deep">{t('Deep — Full power, relaxed timeouts', 'Deep — 完整推理，宽松超时')}</option>
                                        <option value="fast">{t('Fast — Quick reasoning, tight timeouts', 'Fast — 快速推理，紧凑超时')}</option>
                                    </select>
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title" style={{ fontSize: '12px' }}>
                                    {'\u{1F50D}'} {t('Reviewer Reject Budget', '审查员退回上限')}
                                </div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {t(
                                        'How many times reviewer can REJECT and trigger patcher to rewrite + reviewer to re-audit. Each round is reviewer→patcher→reviewer. v7.10+ fully supports multi-round (deadlock-free); 0=single-pass, 2=balanced (recommended), 5=Ultra mode.',
                                        '审查员最多 REJECT 触发补丁师重写 + 再审查的轮数。每轮 = reviewer→patcher→reviewer。v7.10+ 已支持多轮闭环（无死锁）；0=单轮、2=均衡（推荐）、5=Ultra 死磕。'
                                    )}
                                </div>
                                <div className="s-row" style={{ marginTop: 8 }}>
                                    <label style={{ fontWeight: 600 }}>{t('Max Rejections', '最大退回次数')}</label>
                                    <select
                                        className="s-input"
                                        value={reviewerMaxRejections}
                                        onChange={(e) => setReviewerMaxRejections(Number(e.target.value))}
                                        style={{ fontWeight: 600 }}
                                    >
                                        <option value={1}>1 — {t('Single loop (effective; v7.7 default)', '单轮闭环（v7.7 唯一生效配置）')}</option>
                                        <option value={2}>2 — {t('(saved; effective in v7.8)', '（仅保存；v7.8 生效）')}</option>
                                        <option value={3}>3 — {t('(saved; effective in v7.8)', '（仅保存；v7.8 生效）')}</option>
                                        <option value={5}>5 — {t('(saved; effective in v7.8)', '（仅保存；v7.8 生效）')}</option>
                                    </select>
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Per-Node Model Fallback', '节点专属模型回退链')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {t(
                                        'Each node can define its own model priority chain. The runtime will try Priority 1 first, then automatically fall back to the next model on missing key, auth/provider failure, timeout, or network error.',
                                        '每个节点都可以单独定义模型优先级链。运行时会先尝试优先级 1，如果缺少 Key、鉴权失败、提供商异常、超时或网络错误，会自动回退到下一个模型。'
                                    )}
                                </div>
                                <div className="s-hint" style={{ lineHeight: 1.7, marginTop: 6 }}>
                                    {t(
                                        'If Priority 1 is empty, that node keeps using the current global/default model strategy.',
                                        '如果优先级 1 留空，该节点会继续使用当前全局/默认模型策略。'
                                    )}
                                </div>
                            </div>
                            {NODE_MODEL_CONFIG_ROLES.map((role) => {
                                const info = NODE_TYPES[role];
                                const chain = nodeModelPreferences[role] || [];
                                const nodeLabel = lang === 'zh'
                                    ? info?.label_zh || role
                                    : info?.label_en || role;
                                return (
                                    <div key={role} className="s-section">
                                        <div className="s-section-title">{nodeLabel}</div>
                                        {NODE_MODEL_PRIORITY_SLOTS.map((slotIndex) => (
                                            <div className="s-row" key={`${role}-${slotIndex}`}>
                                                <label>{t(`Priority ${slotIndex + 1}`, `优先级 ${slotIndex + 1}`)}</label>
                                                <select
                                                    className="s-input"
                                                    value={chain[slotIndex] || ''}
                                                    onChange={(e) => updateNodeModelPreference(role, slotIndex, e.target.value)}
                                                >
                                                    <option value="">
                                                        {slotIndex === 0
                                                            ? t('Use global/default', '使用全局/默认')
                                                            : t('No fallback', '不设置回退')}
                                                    </option>
                                                    {modelOptions.map((item) => (
                                                        <option key={`${role}-${item.id}`} value={item.id}>
                                                            {item.id} ({PROVIDER_LABELS[item.provider] || item.provider || 'Model'})
                                                        </option>
                                                    ))}
                                                </select>
                                            </div>
                                        ))}
                                        <div className="s-hint" style={{ lineHeight: 1.6 }}>
                                            {chain.length > 0
                                                ? (
                                                    lang === 'zh'
                                                        ? `执行顺序：${chain.join(' → ')}`
                                                        : `Execution order: ${chain.join(' → ')}`
                                                )
                                                : t(
                                                    'No dedicated chain configured for this node.',
                                                    '这个节点当前还没有单独配置模型链。'
                                                )}
                                        </div>
                                    </div>
                                );
                            })}
                            <div className="flex items-center gap-2 mt-2">
                                <button className="btn btn-primary text-[10px]" onClick={saveToBackend} disabled={saving}>
                                    {saving ? t('Saving...', '保存中...') : t('Save Node Model Rules', '保存节点模型规则')}
                                </button>
                                {saveStatus === 'success' && <span className="text-[9px]" style={{ color: 'var(--green)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                {saveStatus === 'error' && <span className="text-[9px]" style={{ color: 'var(--red)' }}>✗ {t('Failed', '保存失败')}</span>}
                            </div>
                        </>
                    )}

                    {tab === 'speed' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">{t('Model Speed Test', '可用模型测速')}</div>
                                <div className="s-hint" style={{ lineHeight: 1.7 }}>
                                    {t(
                                        'Test the latency of all configured models. Only models with valid API keys are tested. Each model receives a trivial prompt with a 15s timeout.',
                                        '测试所有已配置模型的延迟。仅测试有 API Key 的模型。每个模型发送一个简单请求，超时 15 秒。'
                                    )}
                                </div>
                                <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 10 }}>
                                    <button
                                        className="btn btn-primary text-[10px]"
                                        disabled={speedTestRunning}
                                        onClick={async () => {
                                            setSpeedTestRunning(true);
                                            setSpeedTestProgress(lang === 'zh' ? '正在测速...' : 'Testing...');
                                            setSpeedTestResults({});
                                            try {
                                                const resp = await fetch(`${API_BASE}/api/models/speed-test`, {
                                                    method: 'POST',
                                                    headers: { 'Content-Type': 'application/json' },
                                                    body: '{}',
                                                    signal: AbortSignal.timeout(300_000),
                                                });
                                                if (resp.ok) {
                                                    const data = await resp.json();
                                                    setSpeedTestResults(data.results || {});
                                                    setSpeedTestProgress(
                                                        lang === 'zh'
                                                            ? `完成：测试了 ${data.tested_count || 0} / ${data.total_models || 0} 个模型`
                                                            : `Done: tested ${data.tested_count || 0} / ${data.total_models || 0} models`
                                                    );
                                                } else {
                                                    setSpeedTestProgress(lang === 'zh' ? '测速失败' : 'Speed test failed');
                                                }
                                            } catch (err) {
                                                setSpeedTestProgress(lang === 'zh' ? '请求超时或网络错误' : 'Timeout or network error');
                                            } finally {
                                                setSpeedTestRunning(false);
                                            }
                                        }}
                                    >
                                        {speedTestRunning
                                            ? t('Testing...', '测速中...')
                                            : t('Start Speed Test', '开始测速')}
                                    </button>
                                    {speedTestProgress && (
                                        <span className="text-[9px]" style={{ color: 'var(--dimmed)' }}>{speedTestProgress}</span>
                                    )}
                                </div>
                            </div>
                            {Object.keys(speedTestResults).length > 0 && (
                                <div className="s-section">
                                    <div className="s-section-title">{t('Results', '测速结果')}</div>
                                    <div style={{ overflowX: 'auto' }}>
                                        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '10px' }}>
                                            <thead>
                                                <tr style={{ borderBottom: '1px solid var(--border)', textAlign: 'left' }}>
                                                    <th style={{ padding: '6px 8px' }}>{t('Model', '模型')}</th>
                                                    <th style={{ padding: '6px 8px' }}>{t('Provider', '提供商')}</th>
                                                    <th style={{ padding: '6px 8px' }}>{t('Latency', '延迟')}</th>
                                                    <th style={{ padding: '6px 8px' }}>{t('Status', '状态')}</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {Object.entries(speedTestResults)
                                                    .sort((a, b) => {
                                                        // Sort: OK models by latency ascending, then failed models
                                                        if (a[1].ok && !b[1].ok) return -1;
                                                        if (!a[1].ok && b[1].ok) return 1;
                                                        if (a[1].ok && b[1].ok) return a[1].latency_ms - b[1].latency_ms;
                                                        return a[0].localeCompare(b[0]);
                                                    })
                                                    .map(([model, info]) => (
                                                        <tr key={model} style={{ borderBottom: '1px solid var(--border-faint, rgba(255,255,255,0.06))' }}>
                                                            <td style={{ padding: '5px 8px', fontWeight: 500 }}>{model}</td>
                                                            <td style={{ padding: '5px 8px', color: 'var(--dimmed)' }}>
                                                                {PROVIDER_LABELS[info.provider] || info.provider}
                                                            </td>
                                                            <td style={{ padding: '5px 8px' }}>
                                                                {info.ok ? (
                                                                    <span style={{
                                                                        color: info.latency_ms < 3000 ? 'var(--green, #4ade80)' :
                                                                               info.latency_ms < 8000 ? 'var(--yellow, #facc15)' :
                                                                               'var(--red, #f87171)',
                                                                        fontWeight: 600,
                                                                    }}>
                                                                        {info.latency_ms}ms
                                                                    </span>
                                                                ) : (
                                                                    <span style={{ color: 'var(--dimmed)' }}>—</span>
                                                                )}
                                                            </td>
                                                            <td style={{ padding: '5px 8px' }}>
                                                                {info.ok ? (
                                                                    <span style={{ color: 'var(--green, #4ade80)' }}>OK</span>
                                                                ) : info.error === 'no_api_key' ? (
                                                                    <span style={{ color: 'var(--dimmed)' }}>{t('No Key', '未配置')}</span>
                                                                ) : (
                                                                    <span style={{ color: 'var(--red, #f87171)' }} title={info.error}>
                                                                        {t('Failed', '失败')}
                                                                    </span>
                                                                )}
                                                            </td>
                                                        </tr>
                                                    ))}
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            )}
                        </>
                    )}

                    {tab === 'cli' && (
                        <>
                            {/* CLI Mode Toggle */}
                            <div className="s-section">
                                <div className="s-section-title">{t('CLI Mode', 'CLI 模式')}</div>
                                <div className="s-hint" style={{ marginBottom: 8 }}>
                                    {t(
                                        'When enabled, all node AI calls are routed through local CLI tools (Claude Code, Codex, Gemini CLI, etc.) instead of API relay endpoints. This gives direct access, lower latency, and built-in file editing capabilities.',
                                        '开启后，所有节点的 AI 调用将通过本地 CLI 工具（Claude Code、Codex、Gemini CLI 等）执行，而不是通过 API 中转站。这提供直连访问、更低延迟和内置文件编辑能力。',
                                    )}
                                </div>
                                <div className="s-toggle">
                                    <label>{t('Enable CLI Mode', '启用 CLI 模式')}</label>
                                    <input type="checkbox" checked={cliEnabled} onChange={e => setCliEnabled(e.target.checked)} />
                                </div>
                                {/* v7.1i (maintainer 2026-04-25): Ultra Mode toggle.
                                    Was missing in UI — backend supports cli_mode.ultra_mode but
                                    users couldn't see/toggle it. Ultra mode routes every dispatch
                                    through 14-NE pro plan (analyst + uidesign + scribe + 4 builder
                                    parallel + merger + polisher + reviewer + patcher + deployer +
                                    tester + debugger). Without it CLI mode falls back to standard
                                    4-NE plan (builder + reviewer + deployer + tester). */}
                                <div className="s-toggle" style={{ marginTop: 6 }}>
                                    <label>
                                        {t('Ultra Mode', 'Ultra 模式')}
                                        <span style={{ fontSize: 11, opacity: 0.7, marginLeft: 8 }}>
                                            {t(
                                                '14-node DAG: 4 parallel builders + analyst + uidesign + scribe + merger + polisher + reviewer + patcher + deployer + tester + debugger',
                                                '14 节点 DAG：4 并行 builder + analyst + uidesign + scribe + merger + polisher + reviewer + patcher + deployer + tester + debugger',
                                            )}
                                        </span>
                                    </label>
                                    <input
                                        type="checkbox"
                                        checked={cliUltraMode}
                                        disabled={!cliEnabled}
                                        onChange={e => setCliUltraMode(e.target.checked)}
                                    />
                                </div>
                                {/* Save button — prominently placed right after toggle */}
                                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10 }}>
                                    <button
                                        style={{
                                            padding: '6px 18px', fontSize: 13, fontWeight: 600,
                                            background: 'var(--accent, #3b82f6)', color: '#fff',
                                            border: 'none', borderRadius: 6, cursor: 'pointer',
                                            opacity: saving ? 0.6 : 1,
                                        }}
                                        onClick={saveToBackend} disabled={saving}
                                    >
                                        {saving ? t('Saving...', '保存中...') : t('Save CLI Settings', '保存 CLI 设置')}
                                    </button>
                                    {saveStatus === 'success' && <span style={{ fontSize: 12, color: 'var(--green, #4ade80)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                    {saveStatus === 'error' && <span style={{ fontSize: 12, color: 'var(--red, #f87171)' }}>✗ {t('Failed', '保存失败')}</span>}
                                </div>
                            </div>

                            {/* CLI Detection — auto-detect on tab open */}
                            <div className="s-section" ref={(el) => {
                                // Auto-detect CLIs when the tab first appears and nothing was detected yet
                                if (el && Object.keys(cliDetected).length === 0 && !cliDetecting) {
                                    setCliDetecting(true);
                                    fetch(`${API_BASE}/api/cli/detect?force=true`, { credentials: 'omit' })
                                        .then(r => r.ok ? r.json() : null)
                                        .then(data => { if (data?.clis) setCliDetected(data.clis); })
                                        .catch(() => {})
                                        .finally(() => setCliDetecting(false));
                                }
                            }}>
                                <div className="s-section-title">{t('Detected CLI Tools', '已检测的 CLI 工具')}</div>
                                <div className="s-hint" style={{ marginBottom: 8 }}>
                                    {t(
                                        'Scans your system PATH for all known AI CLI tools — registered (Claude Code, Codex, Gemini, Aider) and discovered (Cursor, Copilot, Ollama, etc.).',
                                        '扫描系统 PATH 中所有已知的 AI CLI 工具 — 注册的（Claude Code、Codex、Gemini、Aider）和发现的（Cursor、Copilot、Ollama 等）。',
                                    )}
                                </div>
                                <div className="s-row" style={{ gap: 8 }}>
                                    <button
                                        className="s-btn"
                                        disabled={cliDetecting}
                                        onClick={async () => {
                                            setCliDetecting(true);
                                            try {
                                                const resp = await fetch(`${API_BASE}/api/cli/detect?force=true`, { credentials: 'omit' });
                                                if (resp.ok) {
                                                    const data = await resp.json();
                                                    if (data.clis) setCliDetected(data.clis);
                                                }
                                            } catch { /* offline */ }
                                            setCliDetecting(false);
                                        }}
                                    >
                                        {cliDetecting ? t('Scanning...', '扫描中...') : t('Scan All CLIs', '扫描所有 CLI')}
                                    </button>
                                    <button
                                        className="s-btn"
                                        disabled={cliTestingAll || Object.keys(cliDetected).length === 0}
                                        onClick={async () => {
                                            setCliTestingAll(true);
                                            setCliTestResults({});
                                            try {
                                                const resp = await fetch(`${API_BASE}/api/cli/test-all`, {
                                                    method: 'POST',
                                                    headers: { 'Content-Type': 'application/json' },
                                                    credentials: 'omit',
                                                });
                                                if (resp.ok) {
                                                    const data = await resp.json();
                                                    if (data.results) setCliTestResults(data.results);
                                                }
                                            } catch { /* offline */ }
                                            setCliTestingAll(false);
                                        }}
                                    >
                                        {cliTestingAll ? t('Testing All...', '全部测试中...') : t('Test All', '全部测试')}
                                    </button>
                                </div>

                                {cliDetecting && Object.keys(cliDetected).length === 0 && (
                                    <div style={{ marginTop: 12, opacity: 0.6, fontSize: 12 }}>
                                        {t('Scanning system PATH for AI CLI tools...', '正在扫描系统 PATH 中的 AI CLI 工具...')}
                                    </div>
                                )}
                                {Object.keys(cliDetected).length > 0 && (
                                    <div style={{ marginTop: 12 }}>
                                        <div style={{ fontSize: 11, opacity: 0.5, marginBottom: 6 }}>
                                            {t(
                                                `Found ${Object.values(cliDetected).filter(d => d.available).length} available / ${Object.keys(cliDetected).length} scanned`,
                                                `发现 ${Object.values(cliDetected).filter(d => d.available).length} 个可用 / 共扫描 ${Object.keys(cliDetected).length} 个`,
                                            )}
                                        </div>
                                        <table className="s-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
                                            <thead>
                                                <tr>
                                                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('CLI', 'CLI')}</th>
                                                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('Status', '状态')}</th>
                                                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('Path', '路径')}</th>
                                                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('Version', '版本')}</th>
                                                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('Features', '特性')}</th>
                                                    <th style={{ textAlign: 'center', padding: '6px 8px' }}>{t('Test', '测试')}</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {/* Available CLIs first, then unavailable */}
                                                {Object.entries(cliDetected)
                                                    .sort(([,a], [,b]) => (b.available ? 1 : 0) - (a.available ? 1 : 0))
                                                    .map(([name, info]) => {
                                                    const testResult = cliTestResults[name];
                                                    const isExtra = info.is_extra;
                                                    return (
                                                        <tr key={name} style={{ borderBottom: '1px solid var(--border, #333)', opacity: info.available ? 1 : 0.4 }}>
                                                            <td style={{ padding: '6px 8px', fontWeight: 600 }}>
                                                                {info.display_name}
                                                                {isExtra && <span style={{ fontSize: 9, opacity: 0.5, marginLeft: 4 }}>{t('(discovered)', '(发现)')}</span>}
                                                            </td>
                                                            <td style={{ padding: '6px 8px' }}>
                                                                {info.available
                                                                    ? <span style={{ color: '#4ade80' }}>{t('Available', '可用')}</span>
                                                                    : <span style={{ color: '#f87171' }}>{t('Not Found', '未找到')}</span>
                                                                }
                                                            </td>
                                                            <td style={{ padding: '6px 8px', fontSize: 10, opacity: 0.6, maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={info.path || ''}>
                                                                {info.path || '-'}
                                                            </td>
                                                            <td style={{ padding: '6px 8px', fontSize: 11, opacity: 0.7 }}>
                                                                {info.version || '-'}
                                                            </td>
                                                            <td style={{ padding: '6px 8px', fontSize: 11 }}>
                                                                {[
                                                                    info.supports_json && 'JSON',
                                                                    info.supports_file_ops && t('File Ops', '文件操作'),
                                                                    info.supports_streaming && t('Stream', '流式'),
                                                                ].filter(Boolean).join(', ') || '-'}
                                                            </td>
                                                            <td style={{ padding: '6px 8px', textAlign: 'center' }}>
                                                                {info.available ? (
                                                                    testResult ? (
                                                                        testResult.success
                                                                            ? <span style={{ color: '#4ade80' }}>{testResult.latency_s}s</span>
                                                                            : <span style={{ color: '#f87171' }} title={testResult.error}>FAIL</span>
                                                                    ) : (
                                                                        <button
                                                                            className="s-btn-sm"
                                                                            onClick={async () => {
                                                                                try {
                                                                                    const resp = await fetch(`${API_BASE}/api/cli/test`, {
                                                                                        method: 'POST',
                                                                                        headers: { 'Content-Type': 'application/json' },
                                                                                        credentials: 'omit',
                                                                                        body: JSON.stringify({ cli: name }),
                                                                                    });
                                                                                    if (resp.ok) {
                                                                                        const result = await resp.json();
                                                                                        setCliTestResults(prev => ({ ...prev, [name]: result }));
                                                                                    }
                                                                                } catch { /* offline */ }
                                                                            }}
                                                                        >
                                                                            {t('Test', '测试')}
                                                                        </button>
                                                                    )
                                                                ) : '-'}
                                                            </td>
                                                        </tr>
                                                    );
                                                })}
                                            </tbody>
                                        </table>
                                    </div>
                                )}
                            </div>

                            {/* Global CLI + Model Preference */}
                            <div className="s-section">
                                <div className="s-section-title">{t('Global CLI & Model', '全局 CLI 和模型')}</div>
                                <div className="s-hint" style={{ marginBottom: 8 }}>
                                    {t(
                                        'Set the default CLI and model for all nodes. Per-node overrides below take precedence.',
                                        '设置所有节点的默认 CLI 和模型。下方的单节点设置优先级更高。',
                                    )}
                                </div>
                                <div className="s-row" style={{ gap: 8, alignItems: 'center' }}>
                                    <label style={{ width: 80 }}>{t('CLI', 'CLI')}</label>
                                    <select
                                        className="s-input"
                                        value={cliPreferred}
                                        onChange={e => { setCliPreferred(e.target.value); setCliPreferredModel(''); }}
                                        style={{ width: 'auto', minWidth: 140 }}
                                    >
                                        <option value="">{t('Auto (per node type)', '自动（按节点类型）')}</option>
                                        {Object.entries(cliDetected)
                                            .filter(([, info]) => info.available && !info.is_extra)
                                            .map(([name, info]) => (
                                                <option key={name} value={name}>{info.display_name}</option>
                                            ))
                                        }
                                    </select>
                                    <label style={{ width: 60 }}>{t('Model', '模型')}</label>
                                    <select
                                        className="s-input"
                                        value={cliPreferredModel}
                                        onChange={e => setCliPreferredModel(e.target.value)}
                                        style={{ width: 'auto', minWidth: 160 }}
                                        disabled={!cliPreferred}
                                    >
                                        <option value="">{t('Default', '默认')}</option>
                                        {(cliModelOptions[cliPreferred] || [])
                                            .filter(m => m.id !== '')
                                            .map(m => (
                                                <option key={m.id} value={m.id}>{m.name}</option>
                                            ))
                                        }
                                    </select>
                                </div>
                            </div>

                            {/* Per-node CLI + Model override */}
                            {cliEnabled && Object.values(cliDetected).some(d => d.available) && (
                                <div className="s-section">
                                    <div className="s-section-title">{t('Per-Node CLI & Model', '单节点 CLI 和模型')}</div>
                                    <div className="s-hint" style={{ marginBottom: 8 }}>
                                        {t(
                                            'Override the CLI and model for each node type. Auto inherits the global setting above.',
                                            '为每个节点类型单独指定 CLI 和模型。自动则继承上方的全局设置。',
                                        )}
                                    </div>
                                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                                        <thead>
                                            <tr style={{ borderBottom: '1px solid var(--border, #333)' }}>
                                                <th style={{ textAlign: 'left', padding: '4px 6px', width: 100 }}>{t('Node', '节点')}</th>
                                                <th style={{ textAlign: 'left', padding: '4px 6px' }}>{t('CLI', 'CLI')}</th>
                                                <th style={{ textAlign: 'left', padding: '4px 6px' }}>{t('Model', '模型')}</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {NODE_MODEL_CONFIG_ROLES.map(role => {
                                                const override = cliNodeOverrides[role] || { cli: '', model: '' };
                                                const effectiveCli = override.cli || cliPreferred;
                                                const modelsForCli = cliModelOptions[effectiveCli] || [];
                                                return (
                                                    <tr key={role} style={{ borderBottom: '1px solid var(--border, #222)' }}>
                                                        <td style={{ padding: '4px 6px', fontWeight: 500, textTransform: 'capitalize' }}>{role}</td>
                                                        <td style={{ padding: '4px 6px' }}>
                                                            <select
                                                                className="s-input"
                                                                value={override.cli}
                                                                onChange={e => setCliNodeOverrides(prev => ({
                                                                    ...prev,
                                                                    [role]: { ...prev[role], cli: e.target.value, model: '' },
                                                                }))}
                                                                style={{ width: 'auto', minWidth: 120, fontSize: 11 }}
                                                            >
                                                                <option value="">{t('Auto', '自动')}</option>
                                                                {Object.entries(cliDetected)
                                                                    .filter(([, info]) => info.available && !info.is_extra)
                                                                    .map(([name, info]) => (
                                                                        <option key={name} value={name}>{info.display_name}</option>
                                                                    ))
                                                                }
                                                            </select>
                                                        </td>
                                                        <td style={{ padding: '4px 6px' }}>
                                                            <select
                                                                className="s-input"
                                                                value={override.model}
                                                                onChange={e => setCliNodeOverrides(prev => ({
                                                                    ...prev,
                                                                    [role]: { ...prev[role], cli: prev[role]?.cli || '', model: e.target.value },
                                                                }))}
                                                                style={{ width: 'auto', minWidth: 140, fontSize: 11 }}
                                                            >
                                                                <option value="">{t('Default', '默认')}</option>
                                                                {modelsForCli
                                                                    .filter((m: CLIModelOption) => m.id !== '')
                                                                    .map((m: CLIModelOption) => (
                                                                        <option key={m.id} value={m.id}>{m.name}</option>
                                                                    ))
                                                                }
                                                            </select>
                                                        </td>
                                                    </tr>
                                                );
                                            })}
                                        </tbody>
                                    </table>
                                    {/* Bottom save button — always visible after table */}
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
                                        <button
                                            style={{
                                                padding: '6px 18px', fontSize: 13, fontWeight: 600,
                                                background: 'var(--accent, #3b82f6)', color: '#fff',
                                                border: 'none', borderRadius: 6, cursor: 'pointer',
                                                opacity: saving ? 0.6 : 1,
                                            }}
                                            onClick={saveToBackend} disabled={saving}
                                        >
                                            {saving ? t('Saving...', '保存中...') : t('Save CLI Settings', '保存 CLI 设置')}
                                        </button>
                                        {saveStatus === 'success' && <span style={{ fontSize: 12, color: 'var(--green, #4ade80)' }}>✓ {t('Saved!', '已保存!')}</span>}
                                        {saveStatus === 'error' && <span style={{ fontSize: 12, color: 'var(--red, #f87171)' }}>✗ {t('Failed', '保存失败')}</span>}
                                    </div>
                                </div>
                            )}
                        </>
                    )}

                    {tab === 'ui' && (
                        <>
                            <div className="s-section">
                                <div className="s-section-title">{t('Language', '语言')}</div>
                                <div className="s-row">
                                    <label>{t('UI Language', '界面语言')}</label>
                                    <select className="s-input" value={lang} onChange={e => onLangChange(e.target.value as 'en' | 'zh')} style={{ width: 'auto' }}>
                                        <option value="zh">中文</option>
                                        <option value="en">English</option>
                                    </select>
                                </div>
                                <div className="s-row">
                                    <label>{t('Walkthrough Language', '节点报告语言')}</label>
                                    <select
                                      className="s-input"
                                      value={walkthroughLang}
                                      onChange={e => setWalkthroughLang(e.target.value as '' | 'zh' | 'en')}
                                      style={{ width: 'auto' }}
                                    >
                                        <option value="">{t('Follow UI', '跟随界面语言')}</option>
                                        <option value="zh">{t('Chinese', '中文（纯中文报告）')}</option>
                                        <option value="en">{t('English', 'English (reports in English)')}</option>
                                    </select>
                                </div>
                                <div className="s-hint" style={{ fontSize: 11, color: '#8891a0', marginTop: 4 }}>
                                  {t(
                                    'Node walkthrough reports (the per-node analysis) follow this language. Code identifiers and file paths always stay in English.',
                                    '节点 walkthrough 报告（每个节点的技术分析）使用该语言输出。代码标识符和文件路径保留英文。'
                                  )}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Parallel Builder Strategy', '并行 Builder 策略')}</div>
                                <div className="s-row">
                                    <label style={{ flex: 1 }}>
                                      {t('Share primary model across parallel builders when 2 keys are configured',
                                         '配置了两个 key 时，并行 Builder 共用首选模型')}
                                    </label>
                                    <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                                      <input
                                        type="checkbox"
                                        checked={peerBuildersShareModelWhenMultikey}
                                        onChange={e => setPeerBuildersShareModelWhenMultikey(e.target.checked)}
                                      />
                                      <span>{peerBuildersShareModelWhenMultikey ? t('On', '开') : t('Off', '关')}</span>
                                    </label>
                                </div>
                                <div className="s-hint" style={{ fontSize: 11, color: '#8891a0', marginTop: 4, lineHeight: 1.5 }}>
                                  {t(
                                    'When ON and you\'ve filled both primary + secondary API keys for the top builder provider (e.g. kimi_api_key + kimi_api_key_2), parallel peer builders all use your preferred first model — the two keys round-robin to avoid rate limits. When OFF (or only one key is set), peer builder #2 falls through to the second configured model to avoid hitting one key\'s quota.',
                                    '开启后：若你给首选 Builder 模型填了 primary + secondary 两个 key（比如 kimi_api_key + kimi_api_key_2），并行的多个 Builder 都用你设的第一个模型，两张 key 自动轮流分流，避免单 key 被限速。关闭（或只配 1 个 key）时：第二个 Builder 自动降级到你配的第二个模型，避免把首选 key 撞爆。'
                                  )}
                                </div>
                            </div>
                            <div className="s-section">
                                <div className="s-section-title">{t('Theme', '主题')}</div>
                                <div className="s-row">
                                    <label>{t('Theme', '主题')}</label>
                                    <select className="s-input" value={theme} onChange={e => onThemeChange(e.target.value as 'dark' | 'light')} style={{ width: 'auto' }}>
                                        <option value="dark">{t('Dark', '深色')}</option>
                                        <option value="light">{t('Light', '浅色')}</option>
                                    </select>
                                </div>
                            </div>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}
