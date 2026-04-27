"""Bolt.new-style incremental HTML stream parser.

v6.1.8 — As the LLM streams HTML output (direct_text / direct_multifile
mode), this parser flushes COMPLETE chunks to disk as soon as closing
boundaries land, so the user sees the page materialize in real-time
instead of waiting 170s for the full stream to finish.

Reference:
- Bolt.new `app/lib/runtime/message-parser.ts` (StreamingMessageParser)
- v0 / Lovable use similar chunked-write pattern

Design:
- Pure string state machine (no regex on every chunk — too slow)
- Buffer accumulates chunks; parser scans for CLOSING tag positions
- When a boundary lands, emit everything up to that position
- Remainder stays in buffer for next chunk

Boundaries (in priority order):
1. </html>  → final flush + finalize
2. </body>  → near-complete flush
3. </script> / </style>  → chunk flush (biggest compute blocks)
4. </section> / </main> / </header> / </footer>  → structural flush
5. No boundary yet → keep buffering

Safe for multi-file modes: parser is reset per file via `reset(new_path)`.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("evermind.stream_parser")


FLUSH_BOUNDARIES = (
    "</html>",
    "</body>",
    "</script>",
    "</style>",
    "</section>",
    "</main>",
    "</header>",
    "</footer>",
    "</article>",
)
# Minimum bytes in buffer before we bother scanning — avoids
# O(N*boundaries) on tiny early chunks.
_MIN_SCAN_BYTES = 256
# Force a flush if buffer exceeds this (for streams that never hit a
# boundary — e.g. pure JS file without tags).
_MAX_BUFFER_BYTES = 8192


@dataclass
class ParserStats:
    total_chunks: int = 0
    total_bytes: int = 0
    flushes: int = 0
    flush_bytes: int = 0
    boundaries_hit: List[str] = field(default_factory=list)
    first_flush_ms: float = 0.0
    start_ts: float = 0.0


class IncrementalStreamParser:
    """Feed stream chunks in, get progressive file writes out.

    Usage::

        parser = IncrementalStreamParser(
            target_path="/tmp/evermind_output/task_1/index.html",
            on_flush=lambda path, content, is_final: ...,
        )
        for chunk in stream:
            parser.feed(chunk)
        parser.finalize()

    Thread-safe for single-producer / single-consumer (one stream at a time).
    """

    def __init__(
        self,
        target_path: str = "",
        on_flush: Optional[Callable[[str, str, bool], None]] = None,
        boundaries: tuple = FLUSH_BOUNDARIES,
        max_buffer_bytes: int = _MAX_BUFFER_BYTES,
    ) -> None:
        self.target_path = str(target_path or "")
        self.on_flush = on_flush
        self.boundaries = tuple(boundaries)
        self.max_buffer_bytes = int(max_buffer_bytes)

        self._buffer = ""
        self._flushed_bytes = 0
        self._lock = threading.Lock()
        self.stats = ParserStats()
        import time as _time
        self.stats.start_ts = _time.time()
        self._closed = False

    def reset(self, new_path: str = "") -> None:
        """Reset state for a new file (direct_multifile mode)."""
        with self._lock:
            # Flush any remainder to old file before switching
            if self._buffer and self.target_path:
                self._emit_flush(self._buffer, is_final=True)
                self._buffer = ""
            if new_path:
                self.target_path = str(new_path)
            self._flushed_bytes = 0
            self._closed = False

    def feed(self, chunk: str) -> int:
        """Feed a stream chunk. Returns number of bytes flushed this call."""
        if not chunk or self._closed:
            return 0
        with self._lock:
            self._buffer += str(chunk)
            self.stats.total_chunks += 1
            self.stats.total_bytes += len(chunk)
            # Avoid scanning on every tiny chunk
            if len(self._buffer) < _MIN_SCAN_BYTES:
                return 0
            return self._try_flush()

    def finalize(self) -> int:
        """Flush remaining buffer as final chunk. Returns bytes flushed."""
        with self._lock:
            if self._closed:
                return 0
            flushed = 0
            if self._buffer:
                flushed = len(self._buffer)
                self._emit_flush(self._buffer, is_final=True)
                self._buffer = ""
            self._closed = True
            return flushed

    def _try_flush(self) -> int:
        """Scan buffer for boundaries; flush up to last boundary found."""
        # Find the latest boundary match (flush as much as safely possible)
        latest_end = -1
        latest_boundary = ""
        buf = self._buffer
        lower = buf.lower()
        for b in self.boundaries:
            idx = lower.rfind(b)
            if idx == -1:
                continue
            end = idx + len(b)
            if end > latest_end:
                latest_end = end
                latest_boundary = b
        if latest_end > 0:
            to_flush = buf[:latest_end]
            remainder = buf[latest_end:]
            self._buffer = remainder
            self.stats.boundaries_hit.append(latest_boundary)
            self._emit_flush(to_flush, is_final=False)
            return len(to_flush)
        # No boundary but buffer too large → force flush
        if len(buf) >= self.max_buffer_bytes:
            # Opus B1 fix: retain the last N-1 bytes where N = longest
            # boundary tag so a half-tag (e.g. "</sty") can still resolve
            # on the next chunk. Biggest boundary is "</section>" (10 chars),
            # so retain 10-1 = 9 bytes.
            _retain = max((len(b) for b in self.boundaries), default=10) - 1
            if _retain >= len(buf):
                _retain = 0
            to_flush = buf[:-_retain] if _retain else buf
            self._buffer = buf[-_retain:] if _retain else ""
            if to_flush:
                self._emit_flush(to_flush, is_final=False)
            return len(to_flush)
        return 0

    def _emit_flush(self, content: str, is_final: bool) -> None:
        import time as _time
        if self.stats.flushes == 0 and content:
            self.stats.first_flush_ms = (_time.time() - self.stats.start_ts) * 1000.0
        self.stats.flushes += 1
        self.stats.flush_bytes += len(content)
        if not self.on_flush:
            # Default: append to disk
            self._default_disk_flush(content, is_final)
        else:
            try:
                self.on_flush(self.target_path, content, is_final)
            except Exception:
                logger.exception("on_flush callback failed for %s", self.target_path[:80])
        self._flushed_bytes += len(content)

    def _default_disk_flush(self, content: str, is_final: bool) -> None:
        if not self.target_path:
            return
        try:
            dirpath = os.path.dirname(self.target_path)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            mode = "a" if self._flushed_bytes > 0 else "w"
            with open(self.target_path, mode, encoding="utf-8") as f:
                f.write(content)
        except Exception:
            logger.exception("Disk flush failed for %s", self.target_path[:80])


def parse_multifile_markers(text: str) -> List[dict]:
    """Legacy: extract `<file path="x">...</file>` or ```lang filename blocks.

    Used by direct_multifile mode. Kept for compatibility with existing
    `_direct_multifile_parse` callers.
    """
    import re
    blocks: List[dict] = []
    # Pattern A: <file path="...">content</file>
    for m in re.finditer(
        r'<file\s+path="([^"]+)">(.*?)</file>',
        text, re.DOTALL,
    ):
        blocks.append({"path": m.group(1).strip(), "content": m.group(2)})
    # Pattern B: markdown ```lang filename fenced block
    if not blocks:
        for m in re.finditer(
            r"```(?:html|css|js|javascript|python)\s+([^\n`]+)\n(.+?)```",
            text, re.DOTALL,
        ):
            path = m.group(1).strip()
            if path and "/" not in path and len(path) < 100:
                blocks.append({"path": path, "content": m.group(2)})
    return blocks
