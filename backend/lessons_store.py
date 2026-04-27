"""v5.5 Compound Engineering: persist lessons across pipeline runs.

Inspired by Every Inc.'s Compound Engineering methodology (Kevin Rose, Jan 2026):
"Each unit of engineering work should make subsequent units easier, not harder."

Workflow:
- After every pipeline finishes, reviewer / tester emits blocking_issues
  and required_changes. We distill these into Lesson records keyed by
  task_type (game / website / dashboard / etc.) and persist to jsonl.
- Before a new pipeline starts, the planner injects the top-N most recent
  relevant lessons into its system prompt as "Things that have gone wrong
  before in this task type — DO NOT repeat them".

File format: one JSON object per line at ~/.evermind/compound/lessons.jsonl

Schema:
    {
        "ts": 1713245423,        # unix epoch seconds
        "task_type": "game",     # matches TaskClassifier output
        "task_hint": "...",      # short description of the goal
        "lesson": "...",         # single-sentence takeaway (<=240 chars)
        "source": "reviewer",    # which node emitted this
        "severity": "blocking"   # blocking | warning | info
    }
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("evermind.lessons_store")

_LESSONS_DIR = Path(os.environ.get("EVERMIND_COMPOUND_DIR") or Path.home() / ".evermind" / "compound")
_LESSONS_FILE = _LESSONS_DIR / "lessons.jsonl"

# Cap per task_type so old lessons don't drown out recent ones.
_PER_TASK_TYPE_CAP = int(os.environ.get("EVERMIND_LESSONS_PER_TASK_TYPE_CAP", "40"))
# Total cap — when hit, oldest records are evicted.
_TOTAL_CAP = int(os.environ.get("EVERMIND_LESSONS_TOTAL_CAP", "800"))
# Dedup window: if an identical lesson (same hash) appeared within N seconds,
# skip the new append to avoid jsonl bloat on retry loops.
_DEDUP_WINDOW_SEC = int(os.environ.get("EVERMIND_LESSONS_DEDUP_WINDOW_SEC", "3600"))

_WRITE_LOCK = threading.Lock()


def _ensure_dir() -> None:
    try:
        _LESSONS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Failed to create lessons dir %s: %s", _LESSONS_DIR, exc)


def _hash_lesson(task_type: str, lesson: str) -> str:
    """Stable hash over (task_type, normalized lesson text) for dedup."""
    norm = re.sub(r"\s+", " ", str(lesson or "")).strip().lower()
    raw = f"{task_type or ''}::{norm}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def _read_all() -> List[Dict[str, Any]]:
    if not _LESSONS_FILE.is_file():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with _LESSONS_FILE.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
    except Exception as exc:
        logger.warning("Failed reading lessons file: %s", exc)
    return records


def _write_all(records: Iterable[Dict[str, Any]]) -> None:
    _ensure_dir()
    tmp = _LESSONS_FILE.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(_LESSONS_FILE)
    except Exception as exc:
        logger.warning("Failed writing lessons file: %s", exc)


def _compact(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trim records so total count <= _TOTAL_CAP and each task_type <= _PER_TASK_TYPE_CAP.
    Keeps the most recent ones per task_type."""
    if not records:
        return []
    # Sort by ts ascending (oldest first).
    records.sort(key=lambda r: float(r.get("ts") or 0.0))
    # Per-task_type trim
    per_type: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        per_type.setdefault(str(r.get("task_type") or "default"), []).append(r)
    trimmed: List[Dict[str, Any]] = []
    for rec_list in per_type.values():
        if len(rec_list) > _PER_TASK_TYPE_CAP:
            rec_list = rec_list[-_PER_TASK_TYPE_CAP:]
        trimmed.extend(rec_list)
    trimmed.sort(key=lambda r: float(r.get("ts") or 0.0))
    if len(trimmed) > _TOTAL_CAP:
        trimmed = trimmed[-_TOTAL_CAP:]
    return trimmed


def append(
    task_type: str,
    lesson: str,
    *,
    task_hint: str = "",
    source: str = "",
    severity: str = "info",
) -> bool:
    """Append a lesson to the persistent store. Returns True if written,
    False if deduplicated or input was empty."""
    lesson = str(lesson or "").strip()
    if not lesson:
        return False
    task_type = str(task_type or "default").strip() or "default"
    severity = str(severity or "info").strip().lower() or "info"
    now = time.time()

    with _WRITE_LOCK:
        records = _read_all()
        lesson_hash = _hash_lesson(task_type, lesson)

        # Dedup: skip if an identical lesson recently logged.
        for rec in reversed(records):
            if rec.get("hash") != lesson_hash:
                continue
            try:
                age = now - float(rec.get("ts") or 0.0)
            except Exception:
                age = _DEDUP_WINDOW_SEC + 1
            if age < _DEDUP_WINDOW_SEC:
                return False
            break  # older duplicate — still log fresh

        new_rec = {
            "ts": now,
            "task_type": task_type,
            "task_hint": str(task_hint or "")[:160],
            "lesson": lesson[:240],
            "source": str(source or "")[:32],
            "severity": severity[:16],
            "hash": lesson_hash,
        }
        records.append(new_rec)
        records = _compact(records)
        _write_all(records)
        logger.info(
            "Compound lesson recorded: task_type=%s source=%s severity=%s lesson=%s",
            task_type, source, severity, lesson[:80],
        )
        return True


def relevant(task_type: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Return up to `limit` most-recent lessons matching task_type.
    Prefers blocking > warning > info when we have to truncate."""
    task_type = str(task_type or "default").strip() or "default"
    limit = max(1, min(int(limit or 5), 20))
    records = _read_all()
    matched = [r for r in records if str(r.get("task_type") or "default") == task_type]
    if not matched:
        return []
    sev_priority = {"blocking": 0, "warning": 1, "info": 2}
    matched.sort(key=lambda r: (
        sev_priority.get(str(r.get("severity") or "info"), 3),
        -float(r.get("ts") or 0.0),
    ))
    return matched[:limit]


def stats() -> Dict[str, Any]:
    """Lightweight introspection for UI or debug."""
    records = _read_all()
    per_type: Dict[str, int] = {}
    for r in records:
        per_type[str(r.get("task_type") or "default")] = per_type.get(str(r.get("task_type") or "default"), 0) + 1
    return {
        "total": len(records),
        "per_task_type": per_type,
        "file": str(_LESSONS_FILE),
    }


def prompt_block(task_type: str, limit: int = 5, lang: str = "zh") -> str:
    """Render the relevant lessons as a markdown block suitable for injection
    into a planner / analyst / builder system prompt. Empty string if nothing.
    lang: 'zh' or 'en' — follows the UI language so messaging is localized."""
    lessons = relevant(task_type, limit=limit)
    if not lessons:
        return ""
    is_en = str(lang or "zh").lower().startswith("en")
    header = (
        "\n\n## [分析] Compound Engineering — Lessons From Prior Runs\n"
        "These are real blocking issues encountered on earlier `{t}` tasks. "
        "Avoid repeating them. If your current plan/output might hit the same problem, "
        "adjust proactively.\n"
    ).format(t=task_type) if is_en else (
        "\n\n## [分析] 复合工程 · 过往经验教训\n"
        f"以下是之前 `{task_type}` 任务中真实踩过的坑,**不要再犯**。如果当前计划/输出可能再次触发这些问题,请提前调整。\n"
    )
    items = []
    for idx, rec in enumerate(lessons, start=1):
        sev = str(rec.get("severity") or "info")
        icon = {"blocking": "[停止]", "warning": "[警告]", "info": "ℹ️"}.get(sev, "•")
        hint = str(rec.get("task_hint") or "").strip()
        lesson_text = str(rec.get("lesson") or "").strip()
        line = f"{idx}. {icon} {lesson_text}"
        if hint:
            line += f" _(from: {hint[:80]})_"
        items.append(line)
    return header + "\n".join(items) + "\n"


__all__ = ["append", "relevant", "stats", "prompt_block"]
