'use client';

import { useEffect } from 'react';

const STORAGE_KEYS = [
  'evermind-theme',
  'evermind-runtime',
  'evermind-run-reports-v1',
  'evermind-chat-history-v1',
  'evermind-active-chat-session-v1',
];

function clearLocalState() {
  try {
    for (const key of STORAGE_KEYS) {
      window.localStorage.removeItem(key);
    }
    window.sessionStorage.removeItem('evermind-fresh-session');
  } catch {
    // ignore storage failures
  }
}

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error('[Evermind] Unhandled client error:', error);
  }, [error]);

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
        background: 'radial-gradient(circle at top, rgba(79,143,255,0.14), transparent 42%), #0f1117',
        color: '#f5f7fb',
      }}
    >
      <div
        style={{
          width: 'min(560px, 100%)',
          borderRadius: 20,
          padding: 28,
          border: '1px solid rgba(255,255,255,0.08)',
          background: 'rgba(18, 22, 31, 0.92)',
          boxShadow: '0 30px 70px rgba(0,0,0,0.35)',
        }}
      >
        <div style={{ fontSize: 12, letterSpacing: '0.16em', textTransform: 'uppercase', color: '#7f8aa3', marginBottom: 10 }}>
          Evermind Recovery
        </div>
        <h1 style={{ margin: 0, fontSize: 28, lineHeight: 1.15 }}>
          编辑器加载失败
        </h1>
        <p style={{ marginTop: 12, marginBottom: 0, color: '#b8c0d4', lineHeight: 1.7, fontSize: 14 }}>
          这通常是旧前端缓存或本地持久化状态与当前版本不兼容导致的。可以先重试；如果还是失败，再清理本地界面状态并重新加载。
        </p>
        <div
          style={{
            marginTop: 18,
            padding: 14,
            borderRadius: 14,
            background: 'rgba(255,255,255,0.04)',
            border: '1px solid rgba(255,255,255,0.06)',
            fontSize: 12,
            lineHeight: 1.6,
            color: '#d7dcef',
            wordBreak: 'break-word',
          }}
        >
          {error?.message || 'Unknown client error'}
        </div>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 20 }}>
          <button
            onClick={() => reset()}
            style={{
              border: 'none',
              borderRadius: 12,
              padding: '11px 16px',
              background: '#4f8fff',
              color: '#fff',
              fontWeight: 700,
              cursor: 'pointer',
            }}
          >
            重试加载
          </button>
          <button
            onClick={() => {
              clearLocalState();
              window.location.reload();
            }}
            style={{
              borderRadius: 12,
              padding: '11px 16px',
              background: 'rgba(255,255,255,0.06)',
              border: '1px solid rgba(255,255,255,0.14)',
              color: '#f5f7fb',
              fontWeight: 700,
              cursor: 'pointer',
            }}
          >
            清理本地状态后重载
          </button>
        </div>
      </div>
    </div>
  );
}
