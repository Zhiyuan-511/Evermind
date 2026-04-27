/**
 * v5.6 Context Menu — lightweight, dependency-free right-click menu.
 * Inspired by VS Code's file explorer context menu + the `use-context-menu`
 * React hook pattern popular on GitHub (cluk3/use-context-menu, cdaz5/use-context-menu).
 *
 * Usage:
 *   const [menu, setMenu] = useState<MenuState | null>(null);
 *   <div onContextMenu={e => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY, items: [...] }); }}>
 *   {menu && <ContextMenu {...menu} onClose={() => setMenu(null)} />}
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';

export type ContextMenuItem =
    | {
          kind?: 'item';
          label: string;
          icon?: React.ReactNode;
          shortcut?: string;
          danger?: boolean;
          disabled?: boolean;
          onClick: () => void;
      }
    | { kind: 'separator' };

export type ContextMenuProps = {
    x: number;
    y: number;
    items: ContextMenuItem[];
    onClose: () => void;
};

const MENU_MIN_WIDTH = 200;
const MENU_MAX_WIDTH = 320;

export default function ContextMenu({ x, y, items, onClose }: ContextMenuProps) {
    const containerRef = useRef<HTMLDivElement | null>(null);
    const [position, setPosition] = useState<{ left: number; top: number }>({ left: x, top: y });

    // Flip menu into viewport if it would overflow right/bottom edges.
    useLayoutEffect(() => {
        if (!containerRef.current) return;
        const rect = containerRef.current.getBoundingClientRect();
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        let left = x;
        let top = y;
        if (left + rect.width > vw - 8) left = Math.max(8, vw - rect.width - 8);
        if (top + rect.height > vh - 8) top = Math.max(8, vh - rect.height - 8);
        setPosition({ left, top });
    }, [x, y]);

    useEffect(() => {
        const handleKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        const handleDown = (e: MouseEvent) => {
            if (containerRef.current && !containerRef.current.contains(e.target as Node)) onClose();
        };
        document.addEventListener('keydown', handleKey);
        document.addEventListener('mousedown', handleDown);
        return () => {
            document.removeEventListener('keydown', handleKey);
            document.removeEventListener('mousedown', handleDown);
        };
    }, [onClose]);

    return (
        <div
            ref={containerRef}
            style={{
                position: 'fixed',
                left: position.left,
                top: position.top,
                minWidth: MENU_MIN_WIDTH,
                maxWidth: MENU_MAX_WIDTH,
                background: 'linear-gradient(180deg, #1e2430 0%, #161b22 100%)',
                border: '1px solid rgba(255,255,255,0.08)',
                borderRadius: 8,
                boxShadow: '0 10px 40px rgba(0,0,0,0.55), 0 0 0 1px rgba(0,0,0,0.4)',
                padding: '4px',
                zIndex: 5000,
                fontFamily: 'ui-sans-serif, system-ui, sans-serif',
                fontSize: 12,
                color: '#e6edf3',
                userSelect: 'none',
            }}
            onContextMenu={e => e.preventDefault()}
        >
            {items.map((it, idx) => {
                if ('kind' in it && it.kind === 'separator') {
                    return (
                        <div
                            key={`sep-${idx}`}
                            style={{ height: 1, background: 'rgba(255,255,255,0.06)', margin: '4px 2px' }}
                        />
                    );
                }
                const item = it as Exclude<ContextMenuItem, { kind: 'separator' }>;
                return (
                    <button
                        key={idx}
                        disabled={item.disabled}
                        onClick={() => {
                            if (item.disabled) return;
                            item.onClick();
                            onClose();
                        }}
                        style={{
                            width: '100%',
                            display: 'flex',
                            alignItems: 'center',
                            gap: 10,
                            padding: '7px 10px',
                            border: 'none',
                            borderRadius: 5,
                            background: 'transparent',
                            color: item.disabled ? '#4b5563' : item.danger ? '#f87171' : '#e6edf3',
                            cursor: item.disabled ? 'not-allowed' : 'pointer',
                            fontSize: 12,
                            textAlign: 'left',
                            transition: 'background 120ms ease',
                        }}
                        onMouseEnter={e => {
                            if (!item.disabled)
                                e.currentTarget.style.background = item.danger ? 'rgba(248,113,113,0.12)' : 'rgba(255,255,255,0.06)';
                        }}
                        onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                    >
                        <span style={{ width: 14, height: 14, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', opacity: item.disabled ? 0.4 : 0.85 }}>
                            {item.icon}
                        </span>
                        <span style={{ flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {item.label}
                        </span>
                        {item.shortcut && (
                            <span style={{ fontSize: 10, color: '#6b7280', marginLeft: 8, fontFamily: 'ui-monospace, monospace' }}>
                                {item.shortcut}
                            </span>
                        )}
                    </button>
                );
            })}
        </div>
    );
}
