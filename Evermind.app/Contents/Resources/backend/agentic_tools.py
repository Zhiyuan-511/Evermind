"""
Evermind v3.0 — Agentic Tools Registry
Concrete tool implementations for the AgenticLoop runtime.
Each tool follows the Tool protocol from agentic_runtime.py.

Tools available to nodes:
  - file_read: Read file contents
  - file_write: Write/overwrite file contents
  - file_list: List directory contents
  - grep_search: Search files with ripgrep
  - web_search: Search the web for references
  - web_fetch: Fetch URL content
  - bash: Execute shell commands
  - context_compress: Manually trigger context compression
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evermind.agentic_tools")

# ─── Output directory constant ───
OUTPUT_DIR = Path(os.environ.get("EVERMIND_OUTPUT_DIR", "/tmp/evermind_output"))

# ─── Shared HTTP session for connection pooling ───
# Avoids creating a new TCP+TLS handshake per web_search/web_fetch call
_shared_session: Optional[Any] = None  # aiohttp.ClientSession


async def _get_shared_session() -> Any:
    """Get or create a shared aiohttp.ClientSession with connection pooling."""
    global _shared_session
    import aiohttp
    if _shared_session is None or _shared_session.closed:
        connector = aiohttp.TCPConnector(
            limit=10,           # max simultaneous connections
            ttl_dns_cache=300,  # DNS cache TTL 5 min
            keepalive_timeout=30,
        )
        _shared_session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=20, connect=5),
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Evermind/3.0",
            },
        )
    return _shared_session


async def cleanup_shared_session() -> None:
    """Close the shared session on shutdown. Call from app teardown."""
    global _shared_session
    if _shared_session and not _shared_session.closed:
        await _shared_session.close()
        _shared_session = None


# ═══════════════════════════════════════════════
# Tool interface type
# ═══════════════════════════════════════════════

class ToolResult:
    """Standardized tool execution result."""
    __slots__ = ("success", "output", "error", "metadata", "elapsed_ms")

    def __init__(
        self,
        success: bool = True,
        output: str = "",
        error: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        elapsed_ms: float = 0.0,
    ):
        self.success = success
        self.output = output
        self.error = error
        self.metadata = metadata or {}
        self.elapsed_ms = elapsed_ms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }

    def __repr__(self) -> str:
        status = "✓" if self.success else "✗"
        preview = (self.output or self.error)[:80]
        return f"ToolResult({status}, {self.elapsed_ms:.0f}ms, {preview!r})"


# ═══════════════════════════════════════════════
# Core tool implementations
# ═══════════════════════════════════════════════

async def tool_file_read(path: str, start_line: int = 0, end_line: int = 0, **_kwargs) -> ToolResult:
    """Read file contents with optional line range."""
    t0 = time.monotonic()
    try:
        abs_path = _resolve_safe_path(path)
        if not abs_path:
            return ToolResult(False, error=f"Path not allowed: {path}", elapsed_ms=_elapsed(t0))

        if not abs_path.exists():
            return ToolResult(False, error=f"File not found: {path}", elapsed_ms=_elapsed(t0))
        if not abs_path.is_file():
            return ToolResult(False, error=f"Not a file: {path}", elapsed_ms=_elapsed(t0))

        content = abs_path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)

        if start_line > 0 or end_line > 0:
            s = max(0, start_line - 1)
            e = min(len(lines), end_line) if end_line > 0 else len(lines)
            content = "".join(lines[s:e])
            meta = {"total_lines": len(lines), "shown_start": s + 1, "shown_end": e}
        else:
            meta = {"total_lines": len(lines)}

        # Truncate very large files
        if len(content) > 100_000:
            content = content[:100_000] + f"\n... [truncated, file has {len(lines)} lines total]"
            meta["truncated"] = True

        return ToolResult(True, output=content, metadata=meta, elapsed_ms=_elapsed(t0))
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


async def tool_file_write(path: str, content: str, overwrite: bool = True, **_kwargs) -> ToolResult:
    """Write content to a file. Creates parent directories if needed."""
    t0 = time.monotonic()
    try:
        abs_path = _resolve_safe_path(path)
        if not abs_path:
            return ToolResult(False, error=f"Path not allowed: {path}", elapsed_ms=_elapsed(t0))

        if abs_path.exists() and not overwrite:
            return ToolResult(False, error=f"File already exists: {path}", elapsed_ms=_elapsed(t0))

        existed_before = abs_path.exists()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        return ToolResult(
            True,
            output=f"Wrote {len(content)} bytes to {path}",
            metadata={
                "action": "overwrite" if existed_before else "create",
                "path": str(abs_path),
                "bytes_written": len(content),
            },
            elapsed_ms=_elapsed(t0),
        )
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


async def tool_file_list(path: str = "", max_depth: int = 3, **_kwargs) -> ToolResult:
    """List directory contents recursively."""
    t0 = time.monotonic()
    try:
        target = _resolve_safe_path(path or str(OUTPUT_DIR))
        if not target or not target.is_dir():
            return ToolResult(False, error=f"Not a directory: {path}", elapsed_ms=_elapsed(t0))

        entries: List[str] = []
        _walk_dir(target, target, entries, depth=0, max_depth=max_depth, max_entries=200)

        return ToolResult(
            True,
            output="\n".join(entries) if entries else "(empty directory)",
            metadata={"entry_count": len(entries), "root": str(target)},
            elapsed_ms=_elapsed(t0),
        )
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


async def tool_grep_search(
    query: str,
    path: str = "",
    include: str = "",
    case_insensitive: bool = True,
    max_results: int = 30,
    **_kwargs,
) -> ToolResult:
    """Search files using ripgrep (rg) or fallback grep."""
    t0 = time.monotonic()
    try:
        search_path = _resolve_safe_path(path or str(OUTPUT_DIR))
        if not search_path:
            return ToolResult(False, error=f"Path not allowed: {path}", elapsed_ms=_elapsed(t0))

        # Try ripgrep first, fall back to grep
        rg_cmd = _find_rg()
        if rg_cmd:
            cmd = [rg_cmd, "--json", "-m", str(max_results)]
            if case_insensitive:
                cmd.append("-i")
            if include:
                cmd.extend(["--glob", include])
            cmd.extend([query, str(search_path)])
        else:
            cmd = ["grep", "-rn", "-m", str(max_results)]
            if case_insensitive:
                cmd.append("-i")
            if include:
                cmd.extend(["--include", include])
            cmd.extend([query, str(search_path)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        output = stdout.decode("utf-8", errors="replace")

        if rg_cmd:
            # Parse ripgrep JSON output
            matches = []
            for line in output.splitlines():
                try:
                    data = json.loads(line)
                    if data.get("type") == "match":
                        match_data = data.get("data", {})
                        file_path = match_data.get("path", {}).get("text", "")
                        line_number = match_data.get("line_number", 0)
                        line_text = match_data.get("lines", {}).get("text", "").rstrip()
                        matches.append(f"{file_path}:{line_number}: {line_text}")
                except json.JSONDecodeError:
                    continue
            output = "\n".join(matches[:max_results]) if matches else "No matches found."
        else:
            lines = output.strip().splitlines()[:max_results]
            output = "\n".join(lines) if lines else "No matches found."

        return ToolResult(True, output=output, elapsed_ms=_elapsed(t0))
    except asyncio.TimeoutError:
        return ToolResult(False, error="Grep search timed out after 15s", elapsed_ms=_elapsed(t0))
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


async def tool_web_fetch(url: str, max_chars: int = 30000, **_kwargs) -> ToolResult:
    """Fetch and extract text content from a URL."""
    t0 = time.monotonic()
    try:
        import aiohttp

        session = await _get_shared_session()
        # v3.0 fix: Use ssl=True by default for security.
        # Fallback to ssl=False only for self-signed cert errors.
        try:
            resp = await session.get(url, ssl=True)
        except aiohttp.ClientConnectorCertificateError:
            logger.warning("TLS cert error for %s, retrying with ssl=False", url[:120])
            resp = await session.get(url, ssl=False)
        async with resp:
            if resp.status >= 400:
                return ToolResult(
                    False,
                    error=f"HTTP {resp.status} from {url}",
                    elapsed_ms=_elapsed(t0),
                )
            content_type = resp.headers.get("Content-Type", "")
            raw = await resp.text(errors="replace")

        # Strip HTML tags for readability
        if "html" in content_type.lower():
            raw = _strip_html(raw)

        if len(raw) > max_chars:
            raw = raw[:max_chars] + f"\n... [truncated at {max_chars} chars]"

        return ToolResult(
            True,
            output=raw,
            metadata={"url": url, "chars": len(raw)},
            elapsed_ms=_elapsed(t0),
        )
    except ImportError:
        return ToolResult(False, error="aiohttp not installed", elapsed_ms=_elapsed(t0))
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


async def tool_bash(command: str, timeout_sec: int = 30, cwd: str = "", **_kwargs) -> ToolResult:
    """Execute a shell command with timeout."""
    t0 = time.monotonic()

    # Safety: block dangerous commands
    blocked_patterns = ["rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:", "chmod -R 777 /"]
    cmd_lower = command.lower().strip()
    for pattern in blocked_patterns:
        if pattern in cmd_lower:
            return ToolResult(False, error=f"Blocked dangerous command: {pattern}", elapsed_ms=_elapsed(t0))

    try:
        resolved_cwd: Optional[Path] = None
        if cwd:
            resolved_cwd = _resolve_safe_path(cwd)
            if not resolved_cwd:
                return ToolResult(False, error=f"Path not allowed: {cwd}", elapsed_ms=_elapsed(t0))
            if not resolved_cwd.exists() or not resolved_cwd.is_dir():
                return ToolResult(False, error=f"Not a directory: {cwd}", elapsed_ms=_elapsed(t0))
        elif OUTPUT_DIR.is_dir():
            resolved_cwd = OUTPUT_DIR

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(resolved_cwd) if resolved_cwd else None,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if len(out) > 50_000:
            out = out[:50_000] + "\n... [stdout truncated]"
        if len(err) > 10_000:
            err = err[:10_000] + "\n... [stderr truncated]"

        combined = out
        if err:
            combined += f"\n[stderr]:\n{err}"

        return ToolResult(
            success=proc.returncode == 0,
            output=combined,
            error=err if proc.returncode != 0 else "",
            metadata={
                "exit_code": proc.returncode,
                "command": command[:200],
                "cwd": str(resolved_cwd) if resolved_cwd else "",
            },
            elapsed_ms=_elapsed(t0),
        )
    except asyncio.TimeoutError:
        # v3.0.5 FIX: Kill the orphaned subprocess before returning
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return ToolResult(False, error=f"Command timed out after {timeout_sec}s", elapsed_ms=_elapsed(t0))
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


# ═══════════════════════════════════════════════
# Tool: file_edit (Claude Code FileEditTool port)
# ═══════════════════════════════════════════════

async def tool_file_edit(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    **_kwargs,
) -> ToolResult:
    """Edit a file by replacing exact string matches.

    Modeled after OpenClaude FileEditTool:
    - old_string="" + file doesn't exist = create new file
    - old_string="" + file exists and empty = write new content
    - Exact string match required (no regex)
    - Multiple matches error unless replace_all=True
    """
    t0 = time.monotonic()
    try:
        resolved = _resolve_safe_path(file_path)
        if not resolved:
            return ToolResult(False, error=f"Path not allowed: {file_path}", elapsed_ms=_elapsed(t0))

        # Case 1: Create new file (old_string is empty, file doesn't exist)
        if old_string == "" and not resolved.exists():
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(new_string, encoding="utf-8")
            return ToolResult(
                True,
                output=f"Created new file: {file_path} ({len(new_string)} chars)",
                metadata={"action": "create", "path": str(resolved), "chars_written": len(new_string)},
                elapsed_ms=_elapsed(t0),
            )

        # Case 2: File must exist for edit
        if not resolved.exists():
            return ToolResult(False, error=f"File not found: {file_path}", elapsed_ms=_elapsed(t0))

        content = resolved.read_text(encoding="utf-8", errors="replace")

        # Case 3: old_string is empty but file has content — error
        if old_string == "" and content:
            return ToolResult(
                False,
                error="Cannot create file — file already exists with content. Use old_string to specify what to replace.",
                elapsed_ms=_elapsed(t0),
            )

        # Case 4: old_string is empty, file is empty — write new content
        if old_string == "":
            resolved.write_text(new_string, encoding="utf-8")
            return ToolResult(
                True,
                output=f"Wrote to empty file: {file_path}",
                metadata={"action": "write_empty", "path": str(resolved)},
                elapsed_ms=_elapsed(t0),
            )

        # Case 5: Find and replace
        match_count = content.count(old_string)
        if match_count == 0:
            # Try case-insensitive hint
            ci_count = content.lower().count(old_string.lower())
            hint = f" (found {ci_count} case-insensitive matches)" if ci_count > 0 else ""
            return ToolResult(
                False,
                error=f"String to replace not found in file.{hint}\nString: {old_string[:200]}",
                elapsed_ms=_elapsed(t0),
            )

        if match_count > 1 and not replace_all:
            return ToolResult(
                False,
                error=f"Found {match_count} matches of the string to replace, but replace_all is false. "
                      f"Set replace_all=true to replace all, or provide more context to uniquely identify.",
                elapsed_ms=_elapsed(t0),
            )

        # Perform replacement
        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        resolved.write_text(new_content, encoding="utf-8")

        # Count lines changed
        old_lines = old_string.count("\n") + 1
        new_lines = new_string.count("\n") + 1

        return ToolResult(
            True,
            output=f"Edited {file_path}: replaced {match_count} occurrence(s) ({old_lines} lines → {new_lines} lines)",
            metadata={
                "action": "edit",
                "path": str(resolved),
                "matches_replaced": match_count,
                "old_lines": old_lines,
                "new_lines": new_lines,
            },
            elapsed_ms=_elapsed(t0),
        )
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


# ═══════════════════════════════════════════════
# Tool: web_search (DuckDuckGo-powered)
# ═══════════════════════════════════════════════

async def tool_web_search(
    query: str,
    max_results: int = 8,
    **_kwargs,
) -> ToolResult:
    """Search the web using DuckDuckGo and return structured results.

    Returns title, URL, and snippet for each result.
    No API key required — uses DuckDuckGo HTML search as backend.
    """
    t0 = time.monotonic()
    if not query or len(query.strip()) < 2:
        return ToolResult(False, error="Search query too short (min 2 chars)", elapsed_ms=_elapsed(t0))

    try:
        import aiohttp
        session = await _get_shared_session()
        # Use DuckDuckGo HTML search (no API key needed)
        search_url = "https://html.duckduckgo.com/html/"
        data = {"q": query, "kl": ""}

        async with session.post(search_url, data=data, ssl=True) as resp:
            if resp.status >= 400:
                return ToolResult(False, error=f"Search failed: HTTP {resp.status}", elapsed_ms=_elapsed(t0))
            html = await resp.text(errors="replace")

        # Parse results from DuckDuckGo HTML
        results = []
        # DuckDuckGo HTML results are in <a class="result__a"> tags
        result_pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        for match in result_pattern.finditer(html):
            url = match.group(1).strip()
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            snippet = re.sub(r"<[^>]+>", "", match.group(3)).strip()
            if url and title:
                url = _extract_ddg_url(url)
                results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= max_results:
                break

        if not results:
            # Fallback: try simpler pattern
            link_pattern = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
            for match in link_pattern.finditer(html):
                url = match.group(1).strip()
                title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
                if url and title and "duckduckgo.com" not in url:
                    url = _extract_ddg_url(url)
                    results.append({"title": title, "url": url, "snippet": ""})
                if len(results) >= max_results:
                    break

        if not results:
            return ToolResult(True, output="No results found.", metadata={"query": query, "count": 0}, elapsed_ms=_elapsed(t0))

        # Format results
        lines = [f'Web search results for: "{query}"\n']
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. **{r['title']}**")
            lines.append(f"   URL: {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
            lines.append("")

        return ToolResult(
            True,
            output="\n".join(lines),
            metadata={"query": query, "count": len(results), "results": results},
            elapsed_ms=_elapsed(t0),
        )
    except ImportError:
        return ToolResult(False, error="aiohttp not installed", elapsed_ms=_elapsed(t0))
    except Exception as exc:
        return ToolResult(False, error=f"Search error: {str(exc)[:400]}", elapsed_ms=_elapsed(t0))


# ═══════════════════════════════════════════════
# Tool: glob (File pattern matching)
# ═══════════════════════════════════════════════

async def tool_glob(
    pattern: str,
    path: str = "",
    **_kwargs,
) -> ToolResult:
    """Find files matching a glob pattern. Like 'find' but faster.

    Examples: **/*.js, src/**/*.py, *.html
    """
    t0 = time.monotonic()
    try:
        base = _resolve_safe_path(path) if path else OUTPUT_DIR
        if not base:
            base = OUTPUT_DIR
        if not base.exists():
            return ToolResult(False, error=f"Directory not found: {path or str(OUTPUT_DIR)}", elapsed_ms=_elapsed(t0))

        matches = sorted(str(m) for m in base.glob(pattern))
        if len(matches) > 200:
            truncated = len(matches) - 200
            matches = matches[:200]
            matches.append(f"... and {truncated} more files")

        if not matches:
            return ToolResult(True, output=f"No files matching '{pattern}' in {base}", elapsed_ms=_elapsed(t0))

        return ToolResult(
            True,
            output="\n".join(matches),
            metadata={"pattern": pattern, "count": len(matches), "base": str(base)},
            elapsed_ms=_elapsed(t0),
        )
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


# ═══════════════════════════════════════════════
# Tool: context_compress (Manual trigger)
# ═══════════════════════════════════════════════

async def tool_context_compress(
    level: str = "auto",
    **_kwargs,
) -> ToolResult:
    """Manually trigger context window compression.

    Levels:
    - "auto": Run L1→L2→L3 cascade until context is under threshold
    - "L1": Trim individual tool results only
    - "L2": Fold old conversation turns
    - "L3": Nuclear collapse — compress everything into a summary

    Note: This tool signals the runtime to compress. The actual compression
    is performed by the AgenticLoop after this tool returns.
    """
    t0 = time.monotonic()
    valid_levels = {"auto", "L1", "L2", "L3", "l1", "l2", "l3"}
    normalized = level.strip()
    if normalized not in valid_levels:
        return ToolResult(False, error=f"Invalid level: {level}. Use auto/L1/L2/L3", elapsed_ms=_elapsed(t0))

    return ToolResult(
        True,
        output=f"Context compression requested (level={normalized}). Runtime will compress on next observe step.",
        metadata={"requested_level": normalized.upper(), "signal": "compress_context"},
        elapsed_ms=_elapsed(t0),
    )


# ═══════════════════════════════════════════════
# Tool: multi_file_read (Batch file reading)
# ═══════════════════════════════════════════════

async def tool_multi_file_read(
    paths: str,
    max_chars_per_file: int = 8000,
    **_kwargs,
) -> ToolResult:
    """Read multiple files in one call. Paths separated by newlines or commas.

    More efficient than calling file_read in a loop — reduces iteration count.
    Each file is truncated to max_chars_per_file.
    """
    t0 = time.monotonic()
    try:
        # Parse paths (support newline or comma separation)
        path_list = [p.strip() for p in re.split(r"[,\n]+", paths) if p.strip()]
        if not path_list:
            return ToolResult(False, error="No paths provided", elapsed_ms=_elapsed(t0))
        if len(path_list) > 20:
            path_list = path_list[:20]

        sections = []
        succeeded = 0
        failed = 0
        total_chars = 0

        for file_path in path_list:
            resolved = _resolve_safe_path(file_path)
            if not resolved or not resolved.exists():
                sections.append(f"── {file_path} ── [NOT FOUND]")
                failed += 1
                continue
            if resolved.is_dir():
                sections.append(f"── {file_path} ── [IS DIRECTORY]")
                failed += 1
                continue
            try:
                size = resolved.stat().st_size
                if size > 5 * 1024 * 1024:
                    sections.append(f"── {file_path} ── [TOO LARGE: {_format_size(size)}]")
                    failed += 1
                    continue
                content = resolved.read_text(encoding="utf-8", errors="replace")
                if len(content) > max_chars_per_file:
                    content = content[:max_chars_per_file] + f"\n... [truncated at {max_chars_per_file} chars]"
                sections.append(f"── {file_path} ({_format_size(size)}) ──\n{content}")
                succeeded += 1
                total_chars += len(content)
            except Exception as e:
                sections.append(f"── {file_path} ── [ERROR: {str(e)[:100]}]")
                failed += 1

        return ToolResult(
            True,
            output="\n\n".join(sections),
            metadata={
                "files_read": succeeded,
                "files_failed": failed,
                "total_chars": total_chars,
            },
            elapsed_ms=_elapsed(t0),
        )
    except Exception as exc:
        return ToolResult(False, error=str(exc)[:500], elapsed_ms=_elapsed(t0))


# ═══════════════════════════════════════════════
# Tool Registry
# ═══════════════════════════════════════════════

# Maps tool name -> async function
TOOL_REGISTRY: Dict[str, Any] = {
    "file_read": tool_file_read,
    "read_file": tool_file_read,           # alias
    "file_write": tool_file_write,
    "write_file": tool_file_write,         # alias
    "file_edit": tool_file_edit,
    "file_list": tool_file_list,
    "list_dir": tool_file_list,            # alias
    "grep_search": tool_grep_search,
    "grep": tool_grep_search,              # alias
    "glob": tool_glob,
    "web_fetch": tool_web_fetch,
    "source_fetch": tool_web_fetch,        # alias
    "web_search": tool_web_search,
    "bash": tool_bash,
    "shell": tool_bash,                    # alias
    "context_compress": tool_context_compress,
    "multi_file_read": tool_multi_file_read,
}

# Role-based tool access (which tools each node type can use)
ROLE_TOOL_ACCESS: Dict[str, List[str]] = {
    "planner": ["web_fetch", "web_search", "file_read", "file_list", "glob", "grep_search"],
    "analyst": ["web_fetch", "web_search", "file_read", "file_list", "glob", "grep_search", "multi_file_read"],
    "builder": ["file_read", "file_write", "file_edit", "file_list", "glob", "grep_search",
                "bash", "web_fetch", "web_search", "context_compress", "multi_file_read"],
    "merger": ["file_read", "file_write", "file_edit", "file_list", "glob", "grep_search",
               "bash", "multi_file_read"],
    "polisher": ["file_read", "file_write", "file_edit", "file_list", "glob", "grep_search", "bash"],
    "reviewer": ["file_read", "file_list", "glob", "grep_search", "bash",
                 "web_fetch", "web_search", "multi_file_read"],
    "tester": ["file_read", "file_list", "glob", "grep_search", "bash"],
    "debugger": ["file_read", "file_write", "file_edit", "file_list", "glob", "grep_search",
                 "bash", "web_fetch", "web_search", "context_compress", "multi_file_read"],
    "deployer": ["file_read", "file_list", "bash"],
    "imagegen": ["file_write", "file_list", "web_fetch", "web_search"],
    "spritesheet": ["file_read", "file_list"],
    "assetimport": ["file_read", "file_list"],
    "uidesign": ["web_fetch", "web_search", "file_read", "file_list", "glob"],
    "scribe": ["file_read", "file_write", "file_list", "grep_search"],
}


def get_tools_for_role(role: str) -> Dict[str, Any]:
    """Return the tool functions available for a given node role."""
    normalized = str(role or "").strip().lower()
    tool_names = ROLE_TOOL_ACCESS.get(normalized, ["file_read", "file_list"])
    return {name: TOOL_REGISTRY[name] for name in tool_names if name in TOOL_REGISTRY}


def get_tool_definitions_for_role(role: str) -> List[Dict[str, Any]]:
    """Return OpenAI-compatible tool definitions for a given role."""
    normalized = str(role or "").strip().lower()
    tool_names = ROLE_TOOL_ACCESS.get(normalized, ["file_read", "file_list"])
    definitions = []
    for name in tool_names:
        if name not in TOOL_DEFINITIONS:
            continue
        definitions.append({
            "type": "function",
            "function": TOOL_DEFINITIONS[name],
        })
    return definitions


# OpenAI-compatible tool definitions for LiteLLM
TOOL_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "file_read": {
        "name": "file_read",
        "description": "Read the contents of a file. Supports optional line ranges.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file"},
                "start_line": {"type": "integer", "description": "Start line (1-indexed, optional)"},
                "end_line": {"type": "integer", "description": "End line (1-indexed, optional)"},
            },
            "required": ["path"],
        },
    },
    "file_write": {
        "name": "file_write",
        "description": "Write content to a file. Creates parent directories automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to write to"},
                "content": {"type": "string", "description": "File content to write"},
                "overwrite": {"type": "boolean", "description": "Whether to overwrite existing files (default: true)"},
            },
            "required": ["path", "content"],
        },
    },
    "file_edit": {
        "name": "file_edit",
        "description": "Edit a file by replacing an exact string match. Provide old_string (the exact text to find) and new_string (the replacement). For multiple matches, set replace_all=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file to edit"},
                "old_string": {"type": "string", "description": "The exact string to find and replace. Must match exactly."},
                "new_string": {"type": "string", "description": "The replacement string"},
                "replace_all": {"type": "boolean", "description": "If true, replace all occurrences. Default: false"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "file_list": {
        "name": "file_list",
        "description": "List directory contents recursively.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list"},
                "max_depth": {"type": "integer", "description": "Maximum recursion depth (default: 3)"},
            },
            "required": [],
        },
    },
    "grep_search": {
        "name": "grep_search",
        "description": "Search for a pattern in files using ripgrep.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search pattern"},
                "path": {"type": "string", "description": "Directory or file to search in"},
                "include": {"type": "string", "description": "Glob pattern to filter files (e.g. '*.py')"},
                "case_insensitive": {"type": "boolean", "description": "Case-insensitive search (default: true)"},
            },
            "required": ["query"],
        },
    },
    "glob": {
        "name": "glob",
        "description": "Find files matching a glob pattern. Like 'find' but faster. Examples: **/*.js, src/**/*.py, *.html",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match files (e.g. '**/*.js')"},
                "path": {"type": "string", "description": "Base directory to search from (default: output dir)"},
            },
            "required": ["pattern"],
        },
    },
    "web_fetch": {
        "name": "web_fetch",
        "description": "Fetch and extract text content from a URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_chars": {"type": "integer", "description": "Maximum characters to return (default: 30000)"},
            },
            "required": ["url"],
        },
    },
    "web_search": {
        "name": "web_search",
        "description": "Search the web for information. Returns titles, URLs, and snippets from search results. Use this to find documentation, examples, or solutions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (min 2 characters)"},
                "max_results": {"type": "integer", "description": "Maximum number of results (default: 8, max: 15)"},
            },
            "required": ["query"],
        },
    },
    "bash": {
        "name": "bash",
        "description": "Execute a shell command. Use for build commands, testing, file operations.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout_sec": {"type": "integer", "description": "Timeout in seconds (default: 30)"},
                "cwd": {"type": "string", "description": "Optional working directory. Must be inside an allowed path."},
            },
            "required": ["command"],
        },
    },
    "context_compress": {
        "name": "context_compress",
        "description": "Manually trigger context window compression to free up space. Use when you notice you're running out of context.",
        "parameters": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "description": "Compression level: auto (cascade L1→L2→L3), L1 (trim tool results), L2 (fold old turns), L3 (nuclear collapse). Default: auto"},
            },
            "required": [],
        },
    },
    "multi_file_read": {
        "name": "multi_file_read",
        "description": "Read multiple files in one call. More efficient than reading files one by one.",
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {"type": "string", "description": "File paths separated by newlines or commas"},
                "max_chars_per_file": {"type": "integer", "description": "Max chars per file (default: 8000)"},
            },
            "required": ["paths"],
        },
    },
}


# ═══════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════

def _extract_ddg_url(url: str) -> str:
    """Extract real URL from DuckDuckGo redirect (uddg= parameter)."""
    if "uddg=" in url:
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return parsed.get("uddg", [url])[0]
    return url

def _resolve_safe_path(path: str) -> Optional[Path]:
    """Resolve a path, ensuring it's within allowed directories."""
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = OUTPUT_DIR / p
    # Allow access to output dir, /tmp, home directories for repos
    # macOS: /tmp is a symlink to /private/tmp, so include /private variants
    allowed_roots = [
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/private/var/folders"),
        OUTPUT_DIR,
        Path.home(),
    ]
    resolved = p.resolve()
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except ValueError:
            continue
    return None


def _walk_dir(
    root: Path,
    current: Path,
    entries: List[str],
    depth: int,
    max_depth: int,
    max_entries: int,
) -> None:
    """Recursively walk directory, collecting entries."""
    if depth > max_depth or len(entries) >= max_entries:
        return
    try:
        for item in sorted(current.iterdir()):
            if len(entries) >= max_entries:
                entries.append("... (truncated)")
                return
            rel = item.relative_to(root)
            prefix = "  " * depth
            if item.is_dir():
                entries.append(f"{prefix}{rel}/")
                _walk_dir(root, item, entries, depth + 1, max_depth, max_entries)
            else:
                size = item.stat().st_size
                entries.append(f"{prefix}{rel} ({_format_size(size)})")
    except PermissionError:
        pass


def _format_size(size: int) -> str:
    """Format file size."""
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / (1024 * 1024):.1f}MB"


def _find_rg() -> Optional[str]:
    """Find ripgrep binary."""
    for candidate in ["rg", "/opt/homebrew/bin/rg", "/usr/local/bin/rg"]:
        try:
            subprocess.run([candidate, "--version"], capture_output=True, timeout=3)
            return candidate
        except Exception:
            continue
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and return text content."""
    # Remove script and style blocks
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Normalize whitespace
    html = re.sub(r"\s+", " ", html).strip()
    return html


def _elapsed(t0: float) -> float:
    """Return elapsed milliseconds."""
    return (time.monotonic() - t0) * 1000.0
