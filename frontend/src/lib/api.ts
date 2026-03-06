/* Evermind — REST API Client */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8765';

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
    const res = await fetch(`${API_BASE}${path}`, {
        headers: { 'Content-Type': 'application/json', ...options?.headers },
        ...options,
    });
    if (!res.ok) throw new Error(`API error: ${res.status}`);
    return res.json();
}

// ── Health ──
export const getHealth = () => apiFetch<{ status: string; plugins_loaded: number; clients_connected: number }>('/api/health');

// ── Models ──
export const getModels = () => apiFetch<{ models: Array<{ id: string; provider: string; supports_tools: boolean }> }>('/api/models');

// ── Plugins ──
export const getPlugins = () => apiFetch<{ plugins: Array<{ name: string; display_name: string; description: string; icon: string }> }>('/api/plugins');

export const getPluginDefaults = () => apiFetch<{ defaults: Record<string, string[]> }>('/api/plugins/defaults');

// ── Workflows (future: when database is added) ──
export const getWorkflows = () => apiFetch<{ workflows: unknown[] }>('/api/workflows');
export const createWorkflow = (data: unknown) => apiFetch('/api/workflows', { method: 'POST', body: JSON.stringify(data) });
export const getWorkflow = (id: string) => apiFetch(`/api/workflows/${id}`);
export const updateWorkflow = (id: string, data: unknown) => apiFetch(`/api/workflows/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteWorkflow = (id: string) => apiFetch(`/api/workflows/${id}`, { method: 'DELETE' });
