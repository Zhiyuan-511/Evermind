/* Evermind — Zustand Store (same pattern as Dify/Flowise) */

import { create } from 'zustand';
import { ChatMessage, NodeData, Edge } from '@/lib/types';

interface WorkflowState {
    // Workflow
    nodes: NodeData[];
    edges: Edge[];
    workflowName: string;
    selected: string | null;
    running: boolean;
    lang: 'en' | 'zh';

    // Chat
    messages: ChatMessage[];

    // Connection
    connected: boolean;
    models: unknown[];
    plugins: string[];

    // Actions — Workflow
    setNodes: (nodes: NodeData[]) => void;
    addNode: (node: NodeData) => void;
    updateNode: (id: string, data: Partial<NodeData>) => void;
    removeNode: (id: string) => void;
    setEdges: (edges: Edge[]) => void;
    addEdge: (edge: Edge) => void;
    removeEdge: (from: string, to: string) => void;
    setSelected: (id: string | null) => void;
    setWorkflowName: (name: string) => void;
    setRunning: (running: boolean) => void;
    setLang: (lang: 'en' | 'zh') => void;
    clearWorkflow: () => void;

    // Actions — Chat
    addMessage: (msg: Omit<ChatMessage, 'id'>) => void;

    // Actions — Connection
    setConnected: (connected: boolean) => void;
    setModels: (models: unknown[]) => void;
    setPlugins: (plugins: string[]) => void;
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
    nodes: [],
    edges: [],
    workflowName: 'Workflow 1',
    selected: null,
    running: false,
    lang: 'en',
    messages: [],
    connected: false,
    models: [],
    plugins: [],

    setNodes: (nodes) => set({ nodes }),
    addNode: (node) => set((s) => ({ nodes: [...s.nodes, node] })),
    updateNode: (id, data) => set((s) => ({
        nodes: s.nodes.map((n) => (n.id === id ? { ...n, ...data } : n)),
    })),
    removeNode: (id) => set((s) => ({
        nodes: s.nodes.filter((n) => n.id !== id),
        edges: s.edges.filter((e) => !e.from.startsWith(id) && !e.to.startsWith(id)),
        selected: s.selected === id ? null : s.selected,
    })),
    setEdges: (edges) => set({ edges }),
    addEdge: (edge) => set((s) => ({ edges: [...s.edges, edge] })),
    removeEdge: (from, to) => set((s) => ({
        edges: s.edges.filter((e) => e.from !== from || e.to !== to),
    })),
    setSelected: (selected) => set({ selected }),
    setWorkflowName: (workflowName) => set({ workflowName }),
    setRunning: (running) => set({ running }),
    setLang: (lang) => set({ lang }),
    clearWorkflow: () => set({ nodes: [], edges: [], selected: null }),

    addMessage: (msg) => set((s) => ({
        messages: [...s.messages, {
            ...msg,
            id: Date.now().toString(36) + Math.random().toString(36).slice(2),
        }],
    })),

    setConnected: (connected) => set({ connected }),
    setModels: (models) => set({ models }),
    setPlugins: (plugins) => set({ plugins }),
}));
