"""
OpenClaw Connector V1 — Idempotency Store

Thread-safe in-memory store to deduplicate connector messages.
Each processed idempotency key is stored with its result and a TTL.
On reconnect/retry, the same key returns the cached result without re-executing.
"""

import time
import threading
from typing import Any, Dict, Optional

# Keys older than TTL_SECONDS are eligible for cleanup
TTL_SECONDS = 600  # 10 minutes

# Maximum number of stored keys before forced cleanup
MAX_KEYS = 5000


class IdempotencyEntry:
    __slots__ = ('key', 'result', 'created_at')

    def __init__(self, key: str, result: Any):
        self.key = key
        self.result = result
        self.created_at = time.time()

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > TTL_SECONDS


class IdempotencyStore:
    """Thread-safe in-memory idempotency store with TTL-based cleanup."""

    def __init__(self) -> None:
        self._store: Dict[str, IdempotencyEntry] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> Optional[Any]:
        """
        Check if a key has already been processed.
        Returns the cached result if found and not expired, else None.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expired:
                del self._store[key]
                return None
            return entry.result

    def record(self, key: str, result: Any) -> None:
        """Record a processed idempotency key with its result."""
        with self._lock:
            self._store[key] = IdempotencyEntry(key, result)
            # Periodically clean up expired entries
            if len(self._store) > MAX_KEYS:
                self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        """Remove expired entries. Must be called with lock held."""
        now = time.time()
        expired_keys = [k for k, v in self._store.items() if (now - v.created_at) > TTL_SECONDS]
        for k in expired_keys:
            del self._store[k]

    def has_key(self, key: str) -> bool:
        """Check if key exists and is not expired."""
        return self.check(key) is not None

    def clear(self) -> None:
        """Clear all stored keys."""
        with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        """Number of stored entries (including potentially expired)."""
        return len(self._store)


# Module-level singleton
connector_idempotency = IdempotencyStore()
