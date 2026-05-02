"""v5.5 GitHub repo clone POC — let agents work inside an existing codebase
instead of always greenfield-generating a single HTML page.

Usage:
  - User pastes a GitHub URL in the goal ("Refactor https://github.com/owner/repo …")
  - Orchestrator calls `clone_if_requested(goal)` which detects the URL,
    shallow-clones to a cached directory, and returns the local path.
  - That path is then injected into node configs as `repo_workspace`;
    `build_repo_context` already wires this into analyst/builder prompts.

Cache layout:
    ~/.evermind/repo_cache/
        <owner>__<repo>__<sha-short>/    # a working copy per branch/commit
    ~/.evermind/repo_cache/_manifest.json # url -> path, last_used_at

Safety:
  - Only https:// GitHub URLs are accepted (no ssh, no arbitrary git urls).
  - Depth=1 clone by default to stay fast and small.
  - Hard cap 500MB on-disk (old entries evicted LRU).
  - No writes are pushed back automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("evermind.repo_clone")

CACHE_DIR = Path(os.environ.get("EVERMIND_REPO_CACHE_DIR") or Path.home() / ".evermind" / "repo_cache")
MANIFEST_FILE = CACHE_DIR / "_manifest.json"
MAX_TOTAL_BYTES = int(os.environ.get("EVERMIND_REPO_CACHE_MAX_BYTES", str(500 * 1024 * 1024)))

# Accept only bare github.com/owner/repo URLs. Branches / subdirectories follow
# the "tree/<branch>" convention in GitHub URLs.
_GH_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9][A-Za-z0-9._\-]*)/([A-Za-z0-9][A-Za-z0-9._\-]*)(?:\.git)?"
    r"(?:/tree/([A-Za-z0-9._\-/]+))?",
    re.IGNORECASE,
)


def detect_github_url(goal: str) -> Optional[Tuple[str, str, str, Optional[str]]]:
    """Return (canonical_url, owner, repo, branch) if the goal text contains a
    supported GitHub URL, else None."""
    if not goal:
        return None
    m = _GH_URL_RE.search(str(goal))
    if not m:
        return None
    owner, repo, branch = m.group(1), m.group(2), m.group(3)
    url = f"https://github.com/{owner}/{repo}.git"
    return (url, owner, repo, branch)


def _safe_workdir_name(owner: str, repo: str, branch: Optional[str]) -> str:
    tag = branch or "main"
    safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", tag)[:32] or "main"
    return f"{owner}__{repo}__{safe_tag}"


def _load_manifest() -> Dict[str, Dict[str, Any]]:
    if not MANIFEST_FILE.is_file():
        return {}
    try:
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _save_manifest(data: Dict[str, Dict[str, Any]]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("manifest save failed: %s", exc)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _evict_lru_if_needed() -> None:
    manifest = _load_manifest()
    if not manifest:
        return
    total = sum(int(entry.get("size_bytes") or 0) for entry in manifest.values())
    if total <= MAX_TOTAL_BYTES:
        return
    # Sort by last_used_at ascending (oldest first)
    entries = sorted(manifest.items(), key=lambda kv: float(kv[1].get("last_used_at") or 0.0))
    for url, entry in entries:
        if total <= MAX_TOTAL_BYTES:
            break
        p = Path(str(entry.get("path") or ""))
        if p.is_dir() and CACHE_DIR in p.parents:
            shutil.rmtree(p, ignore_errors=True)
            total -= int(entry.get("size_bytes") or 0)
            manifest.pop(url, None)
            logger.info("Evicted LRU repo cache: %s", p)
    _save_manifest(manifest)


async def clone_or_refresh(url: str, *, depth: int = 1, branch: Optional[str] = None) -> Optional[str]:
    """Clone the repo (shallow) into the cache dir. If already cached, pull
    the latest of its branch. Returns the local working directory, or None
    on failure. Non-blocking: runs git in a subprocess."""
    parsed = detect_github_url(url)
    if not parsed:
        logger.warning("Not a supported GitHub URL: %s", url)
        return None
    canonical_url, owner, repo, found_branch = parsed
    branch = branch or found_branch
    workname = _safe_workdir_name(owner, repo, branch)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / workname

    manifest = _load_manifest()
    entry = manifest.get(canonical_url) or {}
    now = time.time()

    git = shutil.which("git")
    if not git:
        logger.warning("git not found on PATH; cannot clone %s", canonical_url)
        return None

    if target.is_dir() and (target / ".git").is_dir():
        # Refresh: fetch + hard reset to remote default branch
        cmd = [git, "-C", str(target), "fetch", "--depth", str(depth), "origin"]
        if branch:
            cmd.append(branch)
        _run = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(_run.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            _run.kill()
            logger.warning("git fetch timed out for %s", canonical_url)
    else:
        # Fresh clone
        cmd = [git, "clone", "--depth", str(depth)]
        if branch:
            cmd.extend(["--branch", branch, "--single-branch"])
        cmd.extend([canonical_url, str(target)])
        _run = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(_run.wait(), timeout=180.0)
        except asyncio.TimeoutError:
            _run.kill()
            logger.warning("git clone timed out for %s", canonical_url)
            return None
        if _run.returncode != 0:
            err = b""
            try:
                if _run.stderr is not None:
                    err = await _run.stderr.read()
            except Exception:
                pass
            logger.warning("git clone failed for %s: %s", canonical_url, err[:400])
            return None

    size = _dir_size(target)
    manifest[canonical_url] = {
        "path": str(target),
        "owner": owner,
        "repo": repo,
        "branch": branch or "default",
        "size_bytes": size,
        "cloned_at": float(entry.get("cloned_at") or now),
        "last_used_at": now,
    }
    _save_manifest(manifest)
    _evict_lru_if_needed()
    logger.info("Repo ready: %s -> %s (%.1f MB)", canonical_url, target, size / 1024 / 1024)
    return str(target)


def cached_path_for(url: str) -> Optional[str]:
    parsed = detect_github_url(url)
    if not parsed:
        return None
    canonical_url, *_ = parsed
    manifest = _load_manifest()
    entry = manifest.get(canonical_url)
    if not entry:
        return None
    path = str(entry.get("path") or "")
    if path and Path(path).is_dir():
        return path
    return None


def list_cached() -> List[Dict[str, Any]]:
    manifest = _load_manifest()
    out: List[Dict[str, Any]] = []
    for url, entry in sorted(manifest.items(), key=lambda kv: -float(kv[1].get("last_used_at") or 0.0)):
        out.append({
            "url": url,
            "path": entry.get("path"),
            "owner": entry.get("owner"),
            "repo": entry.get("repo"),
            "branch": entry.get("branch"),
            "size_mb": round(float(entry.get("size_bytes") or 0) / 1024 / 1024, 1),
            "last_used_at": entry.get("last_used_at"),
        })
    return out


__all__ = ["detect_github_url", "clone_or_refresh", "cached_path_for", "list_cached"]
