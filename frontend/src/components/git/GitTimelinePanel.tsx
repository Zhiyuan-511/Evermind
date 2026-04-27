"use client";

import React, { useCallback, useEffect, useState } from "react";
import DiffViewer from "./DiffViewer";

interface RunEntry {
  id: string;
  title?: string;
  status?: string;
  created_at?: string;
  stats?: { added?: number; removed?: number };
  diff?: string;
  files?: Array<{ path: string; diff?: string; added?: number; removed?: number }>;
}

interface Props {
  apiBase?: string;
  refreshKey?: number;
}

export default function GitTimelinePanel({ apiBase = "", refreshKey = 0 }: Props) {
  const [runs, setRuns] = useState<RunEntry[]>([]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchRuns = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Backend exposes /api/runs (GET) — list endpoint. Tolerate either
      //   { runs: [...] }  OR  [...]  OR  { items: [...] }.
      const res = await fetch(`${apiBase}/api/runs/list`).catch(() => null)
        ?? await fetch(`${apiBase}/api/runs`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const list: RunEntry[] = Array.isArray(json)
        ? json
        : (json.runs || json.items || []);
      setRuns(list);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    void fetchRuns();
  }, [fetchRuns, refreshKey]);

  const toggle = (id: string) => setExpanded((m) => ({ ...m, [id]: !m[id] }));

  return (
    <div style={{ padding: 8, fontSize: 13 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <strong style={{ color: "#cfd2d6" }}>Git Timeline</strong>
        <button
          type="button"
          onClick={() => void fetchRuns()}
          style={{ background: "transparent", border: "1px solid #3a3a3a", color: "#9aa0a6", borderRadius: 4, padding: "2px 8px", cursor: "pointer" }}
        >
          {loading ? "..." : "Refresh"}
        </button>
      </div>
      {error && <div style={{ color: "#f87171", padding: 8 }}>Failed: {error}</div>}
      {!loading && !error && runs.length === 0 && (
        <div style={{ color: "#9aa0a6", padding: 8 }}>No runs yet.</div>
      )}
      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {runs.map((r) => {
          const add = r.stats?.added ?? r.files?.reduce((s, f) => s + (f.added ?? 0), 0) ?? 0;
          const rem = r.stats?.removed ?? r.files?.reduce((s, f) => s + (f.removed ?? 0), 0) ?? 0;
          const open = !!expanded[r.id];
          return (
            <li key={r.id} style={{ marginBottom: 6, borderLeft: "2px solid #3a3a3a", paddingLeft: 8 }}>
              <button
                type="button"
                onClick={() => toggle(r.id)}
                style={{
                  background: "transparent",
                  border: "none",
                  color: "#d4d4d4",
                  cursor: "pointer",
                  textAlign: "left",
                  width: "100%",
                  padding: "4px 0",
                  display: "flex",
                  flexDirection: "column",
                  gap: 2,
                }}
              >
                <span style={{ fontWeight: 500 }}>{r.title || r.id.slice(0, 12)}</span>
                <span style={{ fontSize: 11, color: "#9aa0a6" }}>
                  {r.status || "unknown"}
                  {" · "}
                  <span style={{ color: "#4ade80" }}>+{add}</span>
                  {" "}
                  <span style={{ color: "#f87171" }}>-{rem}</span>
                  {r.created_at ? ` · ${r.created_at}` : ""}
                </span>
              </button>
              {open && (
                <div style={{ marginTop: 6 }}>
                  {r.files && r.files.length > 0 ? (
                    r.files.map((f) => (
                      <details key={f.path} style={{ marginBottom: 4 }}>
                        <summary style={{ cursor: "pointer", color: "#9aa0a6", fontSize: 12 }}>
                          {f.path} <span style={{ color: "#4ade80" }}>+{f.added ?? 0}</span>{" "}
                          <span style={{ color: "#f87171" }}>-{f.removed ?? 0}</span>
                        </summary>
                        <DiffViewer diff={f.diff || ""} original="" modified="" />
                      </details>
                    ))
                  ) : r.diff ? (
                    <DiffViewer diff={r.diff} original="" modified="" />
                  ) : (
                    <div style={{ color: "#9aa0a6", fontSize: 12, padding: 4 }}>No diff payload.</div>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
