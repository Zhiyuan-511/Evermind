"use client";

import React, { useState } from "react";
import ActivityBar, { ActivityId } from "./ActivityBar";
import SideDock from "./SideDock";

interface Props {
  /** Lazy-rendered panel nodes, keyed by ActivityId. */
  panels: Partial<Record<ActivityId, React.ReactNode>>;
  /** Main editor / canvas content area. */
  children: React.ReactNode;
  defaultActivity?: ActivityId;
}

/**
 * v6.5 Phase 4 shell layout:
 *   [ActivityBar 48px] | [SideDock 240-560px resizable] | [Main content flex:1]
 *
 * Plain CSS flex — no react-resizable-panels dependency.
 * Clicking the active activity icon toggles the dock collapsed state.
 */
export default function AppShell({ panels, children, defaultActivity = "files" }: Props) {
  const [active, setActive] = useState<ActivityId>(defaultActivity);
  const [collapsed, setCollapsed] = useState<boolean>(false);

  const handleChange = (id: ActivityId) => {
    if (id === active) {
      setCollapsed((c) => !c);
    } else {
      setActive(id);
      setCollapsed(false);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "row",
        width: "100%",
        height: "100vh",
        minHeight: 0,
        background: "#1e1e1e",
        color: "#d4d4d4",
        overflow: "hidden",
      }}
    >
      <ActivityBar active={active} onChange={handleChange} />
      <SideDock
        active={active}
        collapsed={collapsed}
        onToggleCollapse={() => setCollapsed(true)}
        panels={panels}
      />
      <main
        style={{
          flex: 1,
          minWidth: 0,
          height: "100%",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {children}
      </main>
    </div>
  );
}
