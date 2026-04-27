"use client";

import React from "react";

export type ActivityId =
  | "files"
  | "git"
  | "chat"
  | "skills"
  | "github"
  | "diagnostics";

export interface ActivityItem {
  id: ActivityId;
  label: string;
  icon: React.ReactNode;
}

interface Props {
  active: ActivityId;
  onChange: (id: ActivityId) => void;
  items?: ActivityItem[];
}

// Inline SVG icons — avoid lucide-react dep (not installed).
const Icon = {
  Files: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <polyline points="13 2 13 9 20 9" />
    </svg>
  ),
  Git: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="18" cy="18" r="3" /><circle cx="6" cy="6" r="3" /><path d="M6 21V9a9 9 0 0 0 9 9" />
    </svg>
  ),
  Chat: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  ),
  Skills: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2l3 7h7l-5.5 4.5L18 22l-6-4-6 4 1.5-8.5L2 9h7z" />
    </svg>
  ),
  GitHub: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22" />
    </svg>
  ),
  Diagnostics: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  ),
};

export const DEFAULT_ACTIVITY_ITEMS: ActivityItem[] = [
  { id: "files", label: "Files", icon: Icon.Files },
  { id: "git", label: "Git Timeline", icon: Icon.Git },
  { id: "chat", label: "Chat", icon: Icon.Chat },
  { id: "skills", label: "Skills", icon: Icon.Skills },
  { id: "github", label: "GitHub", icon: Icon.GitHub },
  { id: "diagnostics", label: "Diagnostics", icon: Icon.Diagnostics },
];

export default function ActivityBar({ active, onChange, items = DEFAULT_ACTIVITY_ITEMS }: Props) {
  return (
    <nav
      aria-label="Activity bar"
      style={{
        width: 48,
        height: "100%",
        background: "#1e1e1e",
        borderRight: "1px solid #2b2b2b",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        padding: "8px 0",
        gap: 4,
        flexShrink: 0,
      }}
    >
      {items.map((it) => {
        const isActive = it.id === active;
        return (
          <button
            key={it.id}
            type="button"
            onClick={() => onChange(it.id)}
            aria-label={it.label}
            title={it.label}
            className="evermind-activity-btn"
            style={{
              width: 40,
              height: 40,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: "transparent",
              color: isActive ? "#ffffff" : "#9aa0a6",
              border: "none",
              borderLeft: isActive ? "2px solid #4f8cff" : "2px solid transparent",
              cursor: "pointer",
              borderRadius: 4,
              transition: "color 120ms, background 120ms",
            }}
            onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.color = "#ffffff")}
            onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.color = isActive ? "#ffffff" : "#9aa0a6")}
          >
            {it.icon}
          </button>
        );
      })}
    </nav>
  );
}
