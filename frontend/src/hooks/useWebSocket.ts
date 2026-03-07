/* Evermind — WebSocket Hook */
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

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

    const clearReconnectTimer = useCallback(() => {
        if (reconnectTimer.current) {
            clearTimeout(reconnectTimer.current);
            reconnectTimer.current = null;
        }
    }, []);

    const scheduleReconnect = useCallback(() => {
        clearReconnectTimer();
        reconnectTimer.current = setTimeout(() => {
            connectRef.current();
        }, reconnectInterval);
    }, [clearReconnectTimer, reconnectInterval]);

    const connect = useCallback(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN || wsRef.current?.readyState === WebSocket.CONNECTING) {
            return;
        }

        try {
            const ws = new WebSocket(url);
            wsRef.current = ws;

            ws.onopen = () => {
                clearReconnectTimer();
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

                    onMessage?.(msg);
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
    }, [url, onMessage, clearReconnectTimer, scheduleReconnect]);

    useEffect(() => {
        connectRef.current = connect;
    }, [connect]);

    const send = useCallback((data: WSMessage) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify(data));
        }
    }, []);

    const sendGoal = useCallback((goal: string, model = 'gpt-5.4') => {
        send({ type: 'run_goal', goal, model });
    }, [send]);

    const runWorkflow = useCallback((nodes: unknown[], edges: unknown[]) => {
        send({ type: 'execute_workflow', nodes, edges });
    }, [send]);

    const stop = useCallback(() => {
        send({ type: 'stop' });
    }, [send]);

    useEffect(() => {
        connect();
        return () => {
            clearReconnectTimer();
            wsRef.current?.close();
            wsRef.current = null;
        };
    }, [connect, clearReconnectTimer]);

    return { connected, models, plugins, send, sendGoal, runWorkflow, stop };
}
