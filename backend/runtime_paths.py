"""
Runtime path helpers shared by backend modules.

These helpers keep Electron and backend environment wiring consistent.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("/tmp/evermind_output")
DEFAULT_STATE_DIR = Path.home() / ".evermind"


def resolve_output_dir() -> Path:
    raw = (
        str(os.getenv("EVERMIND_OUTPUT_DIR") or "").strip()
        or str(os.getenv("OUTPUT_DIR") or "").strip()
        or str(DEFAULT_OUTPUT_DIR)
    )
    return Path(raw).expanduser()


def resolve_state_dir() -> Path:
    raw = str(os.getenv("EVERMIND_STATE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_STATE_DIR


def ensure_output_dir_alias(output_dir: Path, alias: Path = DEFAULT_OUTPUT_DIR) -> Path:
    """
    Keep the legacy /tmp output path pointed at the current runtime output directory.

    This preserves compatibility with older prompts / preview routes while ensuring
    they never read stale artifacts from a different directory.
    """
    target = Path(output_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    legacy = Path(alias).expanduser()
    try:
        target_resolved = target.resolve()
    except Exception:
        target_resolved = target
    try:
        legacy_resolved = legacy.resolve()
    except Exception:
        legacy_resolved = legacy

    if legacy_resolved == target_resolved:
        return legacy

    try:
        if legacy.is_symlink() or legacy.exists():
            try:
                current = legacy.resolve()
            except Exception:
                current = legacy
            if current == target_resolved:
                return legacy
            if legacy.is_symlink() or legacy.is_file():
                legacy.unlink(missing_ok=True)
            else:
                shutil.rmtree(legacy, ignore_errors=True)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.symlink_to(target_resolved, target_is_directory=True)
    except Exception:
        # Best-effort fallback for environments where symlink creation is unavailable.
        legacy.mkdir(parents=True, exist_ok=True)
    return legacy
