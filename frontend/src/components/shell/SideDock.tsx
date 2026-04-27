"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { ActivityId } from "./ActivityBar";

interface Props {
  active: ActivityId;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
  /** Map of activity id -> panel content. Caller owns lazy loading. */
  panels: Partial<Record<ActivityId, React.ReactNode>>;
  minWidth?: number;
  maxWidth?: number;
  defaultWidth?: number;
}

const LS_KEY = "evermind.sideDock.width";

export default function SideDock({
  active,
  collapsed = false,
  onToggleCollapse,
  panels,
  minWidth = 240,
  maxWidth = 560,
  defaultWidth = 320,
}: Props) {
  const [width, setWidth] = useState<number>(() => {
    if (typeof window === "undefined") return defaultWidth;
    const saved = Number(window.localStorage.getItem(LS_KEY) || "0");
    return Number.isFinite(saved) && saved >= minWidth && saved <= maxWidth ? saved : defaultWidth;
  });
  const draggingRef = useRef(false);
  const startRef = useRef<{ x: number; w: number } | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(LS_KEY, String(width));
  }, [width]);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    draggingRef.current = true;
    startRef.current = { x: e.clientX, w: width };
    e.preventDefault();
  }, [width]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!draggingRef.current || !startRef.current) return;
      const dx = e.clientX - startRef.current.x;
      const next = Math.max(minWidth, Math.min(maxWidth, startRef.current.w + dx));
      setWidth(next);
    };
    const onUp = () => {
      draggingRef.current = false;
      startRef.current = null;
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [minWidth, maxWidth]);

  if (collapsed) return null;

  const body = panels[active] ?? (
    <div style={{ padding: 16, color: "#9aa0a6", fontSize: 13 }}>
      No panel registered for <code>{active}</code>.
    </div>
  );

  return (
    <aside
      aria-label={`Side dock: ${active}`}
      style={{
        width,
        minWidth,
        maxWidth,
        height: "100%",
        background: "#252526",
        borderRight: "1px solid #1e1e1e",
        display: "flex",
        flexDirection: "column",
        position: "relative",
        flexShrink: 0,
        color: "#d4d4d4",
      }}
    >
      <header
        style={{
          padding: "8px 12px",
          fontSize: 11,
          textTransform: "uppercase",
          letterSpacing: 0.5,
          color: "#9aa0a6",
          borderBottom: "1px solid #1e1e1e",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <span>{active}</span>
        {onToggleCollapse && (
          <button
            type="button"
            onClick={onToggleCollapse}
            aria-label="Collapse side dock"
            style={{
              background: "transparent",
              border: "none",
              color: "#9aa0a6",
              cursor: "pointer",
              padding: "2px 6px",
            }}
          >
            ×
          </button>
        )}
      </header>
      <div style={{ flex: 1, overflow: "auto" }}>{body}</div>
      {/* drag handle */}
      <div
        role="separator"
        aria-orientation="vertical"
        onMouseDown={onMouseDown}
        style={{
          position: "absolute",
          top: 0,
          right: -2,
          width: 4,
          height: "100%",
          cursor: "col-resize",
          background: "transparent",
          zIndex: 5,
        }}
      />
    </aside>
  );
}
