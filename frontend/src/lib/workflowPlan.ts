import type { Edge as RFEdge, Node } from '@xyflow/react';

const SUPPORTED_RUN_GOAL_NODE_TYPES = new Set([
    'planner',
    'analyst',
    'uidesign',
    'scribe',
    'builder',
    'polisher',
    'patcher',
    'reviewer',
    'deployer',
    'debugger',
    'imagegen',
    'spritesheet',
    'assetimport',
    'merger',
]);

export interface RunGoalPlanNode {
    nodeKey: string;
    nodeLabel: string;
    taskDescription: string;
    dependsOn: string[];
    model?: string;
}

export interface RunGoalPlanPayload {
    source: 'user_canvas';
    nodes: RunGoalPlanNode[];
}

function normalizeCanvasNodeType(value: unknown): string {
    const raw = String(value || '').trim().toLowerCase();
    if (!raw) return '';
    const withoutNumericSuffix = raw.replace(/(?:[_-]?\d+)+$/, '');
    return withoutNumericSuffix || raw;
}

function isUserAuthoredCanvasNode(node: Node): boolean {
    const data = (node.data || {}) as Record<string, unknown>;
    return !data.subtaskId && !data.nodeExecutionId && !data.rawNodeKey;
}

function buildRoleKeys(nodes: Node[]): Map<string, string> {
    const roleCounts = new Map<string, number>();
    const roleIndex = new Map<string, number>();

    for (const node of nodes) {
        const role = normalizeCanvasNodeType((node.data || {}).nodeType);
        if (!role) continue;
        roleCounts.set(role, (roleCounts.get(role) || 0) + 1);
    }

    const keyMap = new Map<string, string>();
    for (const node of nodes) {
        const role = normalizeCanvasNodeType((node.data || {}).nodeType);
        if (!role) continue;
        const count = roleCounts.get(role) || 0;
        const currentIndex = (roleIndex.get(role) || 0) + 1;
        roleIndex.set(role, currentIndex);
        const nodeKey = count <= 1 ? role : `${role}${currentIndex}`;
        keyMap.set(node.id, nodeKey);
    }
    return keyMap;
}

function topologicallySortNodes(nodes: Node[], edges: RFEdge[], keyById: Map<string, string>): Node[] {
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    const indegree = new Map<string, number>();
    const outgoing = new Map<string, string[]>();

    for (const node of nodes) {
        indegree.set(node.id, 0);
        outgoing.set(node.id, []);
    }

    for (const edge of edges) {
        if (!nodeById.has(edge.source) || !nodeById.has(edge.target)) continue;
        outgoing.get(edge.source)?.push(edge.target);
        indegree.set(edge.target, (indegree.get(edge.target) || 0) + 1);
    }

    const queue = nodes
        .filter((node) => (indegree.get(node.id) || 0) === 0)
        .sort((a, b) => {
            const ax = Number(a.position?.x || 0);
            const bx = Number(b.position?.x || 0);
            if (ax !== bx) return ax - bx;
            const ay = Number(a.position?.y || 0);
            const by = Number(b.position?.y || 0);
            if (ay !== by) return ay - by;
            return String(keyById.get(a.id) || a.id).localeCompare(String(keyById.get(b.id) || b.id));
        });

    const ordered: Node[] = [];
    while (queue.length > 0) {
        const current = queue.shift();
        if (!current) break;
        ordered.push(current);
        for (const target of outgoing.get(current.id) || []) {
            const nextDegree = (indegree.get(target) || 0) - 1;
            indegree.set(target, nextDegree);
            if (nextDegree === 0) {
                const targetNode = nodeById.get(target);
                if (targetNode) {
                    queue.push(targetNode);
                    queue.sort((a, b) => {
                        const ax = Number(a.position?.x || 0);
                        const bx = Number(b.position?.x || 0);
                        if (ax !== bx) return ax - bx;
                        const ay = Number(a.position?.y || 0);
                        const by = Number(b.position?.y || 0);
                        if (ay !== by) return ay - by;
                        return String(keyById.get(a.id) || a.id).localeCompare(String(keyById.get(b.id) || b.id));
                    });
                }
            }
        }
    }

    if (ordered.length === nodes.length) return ordered;
    return nodes.slice().sort((a, b) => {
        const ax = Number(a.position?.x || 0);
        const bx = Number(b.position?.x || 0);
        if (ax !== bx) return ax - bx;
        const ay = Number(a.position?.y || 0);
        const by = Number(b.position?.y || 0);
        if (ay !== by) return ay - by;
        return String(keyById.get(a.id) || a.id).localeCompare(String(keyById.get(b.id) || b.id));
    });
}

export function buildRunGoalPlan(nodes: Node[], edges: RFEdge[]): RunGoalPlanPayload | null {
    const userNodes = (nodes || []).filter((node) => {
        if (!isUserAuthoredCanvasNode(node)) return false;
        const role = normalizeCanvasNodeType((node.data || {}).nodeType);
        return SUPPORTED_RUN_GOAL_NODE_TYPES.has(role);
    });

    if (userNodes.length === 0) return null;

    const keyById = buildRoleKeys(userNodes);
    const allowedIds = new Set(userNodes.map((node) => node.id));
    const relevantEdges = (edges || []).filter((edge) => allowedIds.has(edge.source) && allowedIds.has(edge.target));
    const orderedNodes = topologicallySortNodes(userNodes, relevantEdges, keyById);

    const incomingByTarget = new Map<string, string[]>();
    for (const edge of relevantEdges) {
        const sourceKey = keyById.get(edge.source);
        const targetKey = keyById.get(edge.target);
        if (!sourceKey || !targetKey) continue;
        const current = incomingByTarget.get(edge.target) || [];
        if (!current.includes(sourceKey)) current.push(sourceKey);
        incomingByTarget.set(edge.target, current);
    }

    const serializedNodes: RunGoalPlanNode[] = orderedNodes.map((node) => {
        const data = (node.data || {}) as Record<string, unknown>;
        const nodeType = normalizeCanvasNodeType(data.nodeType);
        const nodeKey = keyById.get(node.id) || nodeType;
        const nodeLabel = String(data.label || nodeKey).trim() || nodeKey;
        const taskDescription = String(
            data.taskDescription
            || data.prompt
            || data.lastOutput
            || nodeLabel,
        ).trim() || nodeLabel;
        const model = String(data.model || '').trim();
        return {
            nodeKey,
            nodeLabel,
            taskDescription,
            dependsOn: incomingByTarget.get(node.id) || [],
            ...(model ? { model } : {}),
        };
    });

    if (serializedNodes.length === 0) return null;
    return {
        source: 'user_canvas',
        nodes: serializedNodes,
    };
}
