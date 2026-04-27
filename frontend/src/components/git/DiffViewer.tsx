"use client";

import React, { useMemo } from "react";

interface Props {
  /** Optional unified diff text. If present, takes precedence over original/modified. */
  diff?: string;
  /** Original (left) content — used when diff is not provided. */
  original?: string;
  /** Modified (right) content — used when diff is not provided. */
  modified?: string;
  /** File language hint for Monaco (if we switch to DiffEditor). */
  language?: string;
  height?: number | string;
}

/**
 * Unified-diff viewer. Deliberately uses a colored <pre> because:
 *  - `diff2html` is NOT installed (checked package.json).
 *  - Monaco's `<DiffEditor>` needs `original` + `modified` full strings, not a
 *    unified-diff blob — so we'd need to reconstruct sides; keeping this light
 *    keeps the git timeline render cost bounded even for 30+ files.
 *
 * If the caller passes original + modified instead, we render a simple
 * side-by-side by delegating to Monaco only when @monaco-editor/react is
 * available (lazy-imported to avoid SSR bundle bloat).
 */
export default function DiffViewer({ diff, original, modified, height = 280 }: Props) {
  const lines = useMemo(() => (diff ? diff.split("\n") : []), [diff]);

  if (!diff && (original || modified)) {
    // Lazy side-by-side via Monaco DiffEditor if installed.
    // We use a dynamic require pattern so missing dep still compiles.
    let MonacoDiff: React.ComponentType<{
      original: string;
      modified: string;
      language?: string;
      height?: number | string;
      theme?: string;
      options?: Record<string, unknown>;
    }> | null = null;
    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const mod = require("@monaco-editor/react");
      MonacoDiff = mod.DiffEditor;
    } catch {
      MonacoDiff = null;
    }
    if (MonacoDiff) {
      return (
        <MonacoDiff
          original={original || ""}
          modified={modified || ""}
          language="plaintext"
          height={height}
          theme="vs-dark"
          options={{ readOnly: true, renderSideBySide: true, minimap: { enabled: false } }}
        />
      );
    }
    return (
      <pre style={basePre}>
        {(modified || "").slice(0, 4000)}
      </pre>
    );
  }

  if (!diff) {
    return <div style={{ color: "#9aa0a6", padding: 8, fontSize: 12 }}>No diff.</div>;
  }

  return (
    <pre style={basePre}>
      {lines.map((line, i) => {
        let color = "#d4d4d4";
        let bg: string | undefined;
        if (line.startsWith("+") && !line.startsWith("+++")) {
          color = "#4ade80";
          bg = "rgba(74,222,128,0.08)";
        } else if (line.startsWith("-") && !line.startsWith("---")) {
          color = "#f87171";
          bg = "rgba(248,113,113,0.08)";
        } else if (line.startsWith("@@")) {
          color = "#60a5fa";
        } else if (line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("+++") || line.startsWith("---")) {
          color = "#9aa0a6";
        }
        return (
          <div key={i} style={{ color, background: bg, whiteSpace: "pre", padding: "0 6px" }}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

const basePre: React.CSSProperties = {
  margin: 0,
  padding: 6,
  background: "#1e1e1e",
  border: "1px solid #2b2b2b",
  borderRadius: 4,
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: 12,
  lineHeight: 1.5,
  overflow: "auto",
  maxHeight: 400,
  color: "#d4d4d4",
};
