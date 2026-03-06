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

    const connect = useCallback(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN) return;

        try {
            const ws = new WebSocket(url);

            ws.onopen = () => {
                setConnected(true);
                console.log('[WS] Connected to backend');
            };

            ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data) as WSMessage;

                    // Handle handshake
                    if (msg.type === 'connected') {
                        setModels(msg.models as unknown[] || []);
                        setPlugins(msg.plugins as string[] || []);
                    }

                    onMessage?.(msg);
                } catch {
                    console.error('[WS] Parse error');
                }
            };

            ws.onclose = () => {
                setConnected(false);
                wsRef.current = null;
                // Auto-reconnect
                reconnectTimer.current = setTimeout(connect, reconnectInterval);
            };

            ws.onerror = () => {
                ws.close();
            };

            wsRef.current = ws;
        } catch {
            reconnectTimer.current = setTimeout(connect, reconnectInterval);
        }
    }, [url, onMessage, reconnectInterval]);

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
            reconnectTimer.current && clearTimeout(reconnectTimer.current);
            wsRef.current?.close();
        };
    }, [connect]);

    return { connected, models, plugins, send, sendGoal, runWorkflow, stop };
}
