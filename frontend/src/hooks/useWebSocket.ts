/* Evermind — WebSocket Hook */
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChatAttachment } from '@/lib/types';
import type { RunGoalPlanPayload } from '@/lib/workflowPlan';

interface WSMessage {
    type: string;
    [key: string]: unknown;
}

interface UseWebSocketOptions {
    url: string;
    onMessage?: (msg: WSMessage) => void;
    reconnectInterval?: number;
}

export function useWebSocket({ url, onMessage, reconnectInterval = 3000 }: UseWebSocketOptions) {
    const [connected, setConnected] = useState(false);
    const [models, setModels] = useState<unknown[]>([]);
    const [plugins, setPlugins] = useState<string[]>([]);
    const wsRef = useRef<WebSocket | null>(null);
    const reconnectTimer = useRef<NodeJS.Timeout | null>(null);
    const connectRef = useRef<() => void>(() => undefined);
    const onMessageRef = useRef(onMessage);
    const attemptRef = useRef(0);
    const mountedRef = useRef(true);

    useEffect(() => { onMessageRef.current = onMessage; }, [onMessage]);

    const clearReconnectTimer = useCallback(() => {
        if (reconnectTimer.current) {
            clearTimeout(reconnectTimer.current);
            reconnectTimer.current = null;
        }
    }, []);

    const scheduleReconnect = useCallback(() => {
        clearReconnectTimer();
        const backoff = Math.min(reconnectInterval * Math.pow(2, attemptRef.current), 30_000);
        const jitter = Math.random() * backoff * 0.3;
        attemptRef.current += 1;
        reconnectTimer.current = setTimeout(() => {
            connectRef.current();
        }, backoff + jitter);
    }, [clearReconnectTimer, reconnectInterval]);

    const connect = useCallback(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN || wsRef.current?.readyState === WebSocket.CONNECTING) {
            return;
        }
        if (!mountedRef.current) return;

        try {
            const ws = new WebSocket(url);
            wsRef.current = ws;

            ws.onopen = () => {
                clearReconnectTimer();
                attemptRef.current = 0;
                setConnected(true);
                console.log('[WS] Connected to backend');
            };

            ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data) as WSMessage;

                    if (msg.type === 'connected') {
                        setModels((msg.models as unknown[]) || []);
                        setPlugins((msg.plugins as string[]) || []);
                    }

                    onMessageRef.current?.(msg);
                } catch {
                    console.error('[WS] Parse error');
                }
            };

            ws.onclose = () => {
                setConnected(false);
                wsRef.current = null;
                scheduleReconnect();
            };

            ws.onerror = () => {
                ws.close();
            };
        } catch {
            scheduleReconnect();
        }
    }, [url, clearReconnectTimer, scheduleReconnect]);

    useEffect(() => {
        connectRef.current = connect;
    }, [connect]);

    const send = useCallback((data: WSMessage) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify(data));
        }
    }, []);

    const sendGoal = useCallback((
        goal: string,
        model = 'kimi-coding',
        chatHistory?: Array<{role: string; content: string}>,
        difficulty = 'standard',
        runtime: 'local' | 'openclaw' = 'local',
        sessionId = '',
        attachments: ChatAttachment[] = [],
        plan: RunGoalPlanPayload | null = null,
    ) => {
        send({
            type: 'run_goal',
            goal,
            model,
            chat_history: chatHistory || [],
            difficulty,
            runtime,
            session_id: sessionId,
            attachments,
            ...(plan ? { plan } : {}),
        });
    }, [send]);

    const runWorkflow = useCallback((nodes: unknown[], edges: unknown[]) => {
        send({ type: 'execute_workflow', nodes, edges });
    }, [send]);

    const stop = useCallback(() => {
        send({ type: 'stop' });
    }, [send]);

    const reconnect = useCallback(() => {
        clearReconnectTimer();
        const current = wsRef.current;
        if (current) {
            try {
                current.onclose = null;
                current.close();
            } catch {
                /* ignore */
            }
            wsRef.current = null;
        }
        setConnected(false);
        setTimeout(() => {
            if (mountedRef.current) connectRef.current();
        }, 50);
    }, [clearReconnectTimer]);

    useEffect(() => {
        mountedRef.current = true;
        connect();
        return () => {
            mountedRef.current = false;
            clearReconnectTimer();
            wsRef.current?.close();
            wsRef.current = null;
        };
    }, [connect, clearReconnectTimer]);

    return { connected, models, plugins, send, sendGoal, runWorkflow, stop, reconnect, wsRef };
}
