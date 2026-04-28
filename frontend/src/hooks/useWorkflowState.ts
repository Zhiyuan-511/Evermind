'use client';

import { useCallback, useMemo, useState } from 'react';
import {
    addEdge, useNodesState, useEdgesState,
    type Connection, type Node, type Edge as RFEdge,
} from '@xyflow/react';
import { NODE_TYPES } from '@/lib/types';
import type { TemplateDef } from '@/components/TemplateGallery';

let nodeCounter = 0;
function genId() { return 'n_' + (++nodeCounter) + '_' + Date.now().toString(36); }
function fallbackNodeInfo(type: string) {
    const normalized = String(type || 'node').trim() || 'node';
    const title = normalized
        .replace(/[_-]+/g, ' ')
        .replace(/\b\w/g, (ch) => ch.toUpperCase());
    return {
        icon: title.slice(0, 2).toUpperCase(),
        color: '#64748b',
        label_en: title,
        label_zh: title,
        desc_en: 'Custom Node',
        desc_zh: '自定义节点',
        inputs: [{ id: 'input', label: 'Input' }],
        outputs: [{ id: 'output', label: 'Output' }],
    };
}

// Layout constants for auto-created nodes
const AUTO_NODE_X_START = 80;
const AUTO_NODE_X_GAP = 320;
const AUTO_NODE_Y_CENTER = 250;
const AUTO_NODE_Y_GAP = 220;

export interface UseWorkflowStateReturn {
    nodes: Node[];
    setNodes: ReturnType<typeof useNodesState<Node>>[1];
    onNodesChange: ReturnType<typeof useNodesState<Node>>[2];
    edges: RFEdge[];
    setEdges: ReturnType<typeof useEdgesState<RFEdge>>[1];
    onEdgesChange: ReturnType<typeof useEdgesState<RFEdge>>[2];
    onConnect: (conn: Connection) => void;
    onDragOver: (e: React.DragEvent) => void;
    onDrop: (e: React.DragEvent) => void;
    handleSidebarDragStart: (type: string) => void;
    handleLoadTemplate: (tpl: TemplateDef) => void;
    handleExport: (workflowName: string) => void;
    handleClear: () => void;
    defaultEdgeOptions: { animated: boolean; style: { stroke: string; strokeWidth: number } };
    /**
     * Auto-create visual nodes from an orchestrator plan.
     * Returns the subtaskId → canvasNodeId mapping.
     */
    buildPlanNodes: (
        subtasks: Array<{ id: string; agent: string; task: string; depends_on: string[] }>,
        lang: 'en' | 'zh',
    ) => Record<string, string>;
    /**
     * Update a canvas node's data by its ID.
     */
    updateNodeData: (nodeId: string, data: Record<string, unknown>) => void;
}

export function useWorkflowState(lang: 'en' | 'zh'): UseWorkflowStateReturn {
    const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState<RFEdge>([]);
    const [draggingType, setDraggingType] = useState<string | null>(null);

    const defaultEdgeOptions = useMemo(() => ({
        animated: true,
        style: { stroke: 'var(--edge-color)', strokeWidth: 2 },
    }), []);

    // ── Connect edges ──
    const onConnect = useCallback((conn: Connection) => {
        setEdges(eds => addEdge({ ...conn, animated: true, style: { stroke: 'var(--edge-color)', strokeWidth: 2 } }, eds));
    }, [setEdges]);

    // ── Drag from sidebar ──
    const handleSidebarDragStart = useCallback((type: string) => {
        setDraggingType(type);
    }, []);

    const onDragOver = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
    }, []);

    const onDrop = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        const type = draggingType;
        if (!type) return;
        const info = NODE_TYPES[type];
        if (!info) return;
        const reactFlowBounds = (e.target as HTMLElement).closest('.react-flow')?.getBoundingClientRect();
        if (!reactFlowBounds) return;
        const x = e.clientX - reactFlowBounds.left;
        const y = e.clientY - reactFlowBounds.top;
        const newNode: Node = {
            id: genId(), type: 'agent',
            position: { x, y },
            data: {
                nodeType: type,
                label: lang === 'zh' ? info.label_zh : info.label_en,
                status: 'idle', progress: 0, model: 'gpt-5.4', lastOutput: '', lang,
            },
        };
        setNodes(nds => [...nds, newNode]);
        setDraggingType(null);
    }, [draggingType, lang, setNodes]);

    // ── Load template ──
    const handleLoadTemplate = useCallback((tpl: TemplateDef) => {
        const newNodes: Node[] = [];
        tpl.nodes.forEach((nd, idx) => {
            const info = NODE_TYPES[nd.type] || fallbackNodeInfo(nd.type);
            const id = genId();
            const count = tpl.nodes.slice(0, idx).filter(n => n.type === nd.type).length;
            const label = (lang === 'zh' ? info.label_zh : info.label_en) + (count ? ` #${count + 1}` : '');
            newNodes.push({
                id, type: 'agent',
                position: { x: nd.x, y: nd.y },
                data: { nodeType: nd.type, label, status: 'idle', progress: 0, model: 'gpt-5.4', lastOutput: '', lang },
            });
        });
        const newEdges: RFEdge[] = [];
        tpl.edges.forEach(([fromIdx, toIdx]) => {
            const fromNode = newNodes[fromIdx];
            const toNode = newNodes[toIdx];
            if (!fromNode || !toNode) return;
            const fromInfo = NODE_TYPES[tpl.nodes[fromIdx].type] || fallbackNodeInfo(tpl.nodes[fromIdx].type);
            const toInfo = NODE_TYPES[tpl.nodes[toIdx].type] || fallbackNodeInfo(tpl.nodes[toIdx].type);
            newEdges.push({
                id: `e_${fromNode.id}_${toNode.id}`,
                source: fromNode.id, target: toNode.id,
                sourceHandle: fromInfo.outputs[0]?.id || null,
                targetHandle: toInfo.inputs[0]?.id || null,
                animated: true, style: { stroke: 'var(--edge-color)', strokeWidth: 2 },
            });
        });
        setNodes(newNodes);
        setEdges(newEdges);
    }, [lang, setNodes, setEdges]);

    // ── Build plan nodes from orchestrator ──
    const buildPlanNodes = useCallback((
        subtasks: Array<{ id: string; agent: string; task: string; depends_on: string[] }>,
        planLang: 'en' | 'zh',
    ): Record<string, string> => {
        const newNodeMap: Record<string, string> = {};
        const newNodes: Node[] = [];
        const newEdges: RFEdge[] = [];

        // Build dependency graph for layout
        const depthMap: Record<string, number> = {};
        const calcDepth = (id: string, visited: Set<string> = new Set()): number => {
            if (depthMap[id] !== undefined) return depthMap[id];
            if (visited.has(id)) return 0;
            visited.add(id);
            const st = subtasks.find(s => s.id === id);
            if (!st || !st.depends_on?.length) { depthMap[id] = 0; return 0; }
            const maxDep = Math.max(...st.depends_on.map(d => calcDepth(d, visited)));
            depthMap[id] = maxDep + 1;
            return depthMap[id];
        };
        subtasks.forEach(st => calcDepth(st.id));

        const depthGroups: Record<number, typeof subtasks> = {};
        subtasks.forEach(st => {
            const d = depthMap[st.id] ?? 0;
            if (!depthGroups[d]) depthGroups[d] = [];
            depthGroups[d].push(st);
        });

        subtasks.forEach(st => {
            const agentType = st.agent || 'builder';
            const info = NODE_TYPES[agentType] || fallbackNodeInfo(agentType);
            const nodeId = genId();
            newNodeMap[st.id] = nodeId;
            const depth = depthMap[st.id] ?? 0;
            const group = depthGroups[depth] || [st];
            const idxInGroup = group.indexOf(st);
            const groupSize = group.length;
            const yOffset = (idxInGroup - (groupSize - 1) / 2) * AUTO_NODE_Y_GAP;
            newNodes.push({
                id: nodeId, type: 'agent',
                position: { x: AUTO_NODE_X_START + depth * AUTO_NODE_X_GAP, y: AUTO_NODE_Y_CENTER + yOffset },
                data: {
                    nodeType: agentType,
                    // v7.7: rawNodeKey lets resolveCanvasNodeId match by nodeKey
                    // even when no exact nodeExecutionId binding has happened yet
                    // (chat-posted goal race). Without this, builder1/builder2/etc.
                    // multi-instance same-type nodes fail rawKeyMatches uniqueness.
                    rawNodeKey: agentType,
                    nodeExecutionId: '',
                    label: `${planLang === 'zh' ? (info.label_zh || info.label_en) : info.label_en} #${st.id}`,
                    status: 'idle', progress: 0, model: 'kimi-k2.5', lastOutput: '',
                    lang: planLang, subtaskId: st.id, taskDescription: st.task,
                },
            });
        });

        subtasks.forEach(st => {
            (st.depends_on || []).forEach(depId => {
                const sourceId = newNodeMap[depId];
                const targetId = newNodeMap[st.id];
                if (sourceId && targetId) {
                    const srcSt = subtasks.find(s => s.id === depId);
                    const srcInfo = NODE_TYPES[srcSt?.agent || 'builder'] || fallbackNodeInfo(srcSt?.agent || 'builder');
                    const tgtInfo = NODE_TYPES[st.agent || 'builder'] || fallbackNodeInfo(st.agent || 'builder');
                    newEdges.push({
                        id: `e_auto_${sourceId}_${targetId}`,
                        source: sourceId, target: targetId,
                        sourceHandle: srcInfo?.outputs[0]?.id || null,
                        targetHandle: tgtInfo?.inputs[0]?.id || null,
                        animated: true, style: { stroke: 'var(--edge-color)', strokeWidth: 2 },
                    });
                }
            });
        });

        setNodes(newNodes);
        setEdges(newEdges);
        return newNodeMap;
    }, [setNodes, setEdges]);

    // ── Update single node data ──
    const updateNodeData = useCallback((nodeId: string, data: Record<string, unknown>) => {
        setNodes(nds => nds.map(n => n.id === nodeId ? { ...n, data: { ...n.data, ...data } } : n));
    }, [setNodes]);

    const handleExport = useCallback((workflowName: string) => {
        const data = { name: workflowName, nodes, edges, exported_at: new Date().toISOString(), version: '2.0' };
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url; a.download = `${workflowName.replace(/\s+/g, '_')}.json`; a.click();
        URL.revokeObjectURL(url);
    }, [nodes, edges]);

    const handleClear = useCallback(() => { setNodes([]); setEdges([]); }, [setNodes, setEdges]);

    return {
        nodes, setNodes, onNodesChange,
        edges, setEdges, onEdgesChange,
        onConnect, onDragOver, onDrop,
        handleSidebarDragStart, handleLoadTemplate, handleExport, handleClear,
        defaultEdgeOptions, buildPlanNodes, updateNodeData,
    };
}
