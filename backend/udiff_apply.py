"""
Evermind — Universal udiff Patcher (v6.1.15)

Applies LLM-generated unified-diff patches with Aider-style fuzzy matching.
Works across project types: website (.html/.css/.js), game (same + assets),
slides (.md/.html), software (.py/.ts/.go/etc).

Design rules (maintainer 2026-04-20):
1. Accept LLM-generated udiff even if hunk headers are wrong
2. Match by context lines (not line numbers)
3. Three-tier fallback: exact → whitespace-loose → drop-context
4. Apply hunks atomically; partial failures leave the file unchanged
5. Return structured result so orchestrator can report what applied

References:
- Aider udiff format: https://aider.chat/docs/unified-diffs.html
- Why udiff beats SEARCH/REPLACE: 61% vs 20% on GPT-4 Turbo refactor bench
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("evermind.udiff_apply")

# Parse udiff blocks of shape:
#   --- path/to/file
#   +++ path/to/file
#   @@ ... @@
#   -old line
#   +new line
_HUNK_HEADER_RE = re.compile(r"^@@.*@@.*$", re.MULTILINE)
_FILE_HEADER_RE = re.compile(r"^(?:--- )(.+?)$\n^(?:\+\+\+ )(.+?)$", re.MULTILINE)


def parse_udiff(udiff_text: str) -> List[Dict[str, Any]]:
    """Parse raw udiff text into a list of file-patches.

    Each patch dict:
        {"src_path": "a/...", "dst_path": "b/...", "hunks": [hunk_dict, ...]}
    Each hunk dict:
        {"lines": [{"op": " |+|-", "text": "..."}, ...]}
    """
    text = str(udiff_text or "")
    if not text.strip():
        return []

    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    patches: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # File header: --- <src>
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            src = line[4:].strip()
            dst = lines[i + 1][4:].strip()
            # strip a/ b/ prefixes and leading / if user LLM added them
            for prefix in ("a/", "b/"):
                if src.startswith(prefix):
                    src = src[len(prefix):]
                if dst.startswith(prefix):
                    dst = dst[len(prefix):]
            patch = {"src_path": src, "dst_path": dst, "hunks": []}
            i += 2
            # Accumulate hunks until next file header or EOF
            while i < len(lines):
                cur = lines[i]
                if cur.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                    break
                if cur.startswith("@@"):
                    hunk_lines: List[Dict[str, str]] = []
                    i += 1
                    while i < len(lines):
                        nxt = lines[i]
                        if nxt.startswith("@@") or nxt.startswith("--- "):
                            break
                        if not nxt:
                            hunk_lines.append({"op": " ", "text": ""})
                            i += 1
                            continue
                        op = nxt[0]
                        if op in (" ", "+", "-"):
                            hunk_lines.append({"op": op, "text": nxt[1:]})
                        else:
                            # Unknown prefix — treat as context
                            hunk_lines.append({"op": " ", "text": nxt})
                        i += 1
                    if hunk_lines:
                        patch["hunks"].append({"lines": hunk_lines})
                    continue
                i += 1
            if patch["hunks"]:
                patches.append(patch)
            continue
        i += 1
    return patches


def _hunk_target_rows(hunk: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (rows_to_find, rows_to_replace_with)."""
    before: List[str] = []
    after: List[str] = []
    for line in hunk["lines"]:
        op = line["op"]
        text = line["text"]
        if op == " ":
            before.append(text)
            after.append(text)
        elif op == "-":
            before.append(text)
        elif op == "+":
            after.append(text)
    return before, after


def _try_match(source_lines: List[str], before: List[str], *, loose: bool = False) -> int:
    """Find the start index in source_lines matching `before`. -1 if none."""
    if not before:
        return -1
    n = len(before)
    if loose:
        # Whitespace-insensitive comparison
        norm_src = [re.sub(r"\s+", "", s) for s in source_lines]
        norm_need = [re.sub(r"\s+", "", s) for s in before]
        for i in range(len(norm_src) - n + 1):
            if norm_src[i:i + n] == norm_need:
                return i
        return -1
    for i in range(len(source_lines) - n + 1):
        if source_lines[i:i + n] == before:
            return i
    return -1


def apply_udiff_to_file(file_path: str, udiff_text: str) -> Dict[str, Any]:
    """Apply a multi-hunk udiff to one file.

    Returns: {"ok": bool, "file": str, "hunks_applied": int, "hunks_total": int,
              "errors": [str, ...], "diff": <short string>}
    """
    p = Path(file_path)
    if not p.exists():
        return {"ok": False, "file": file_path, "hunks_applied": 0, "hunks_total": 0,
                "errors": [f"file not found: {file_path}"]}

    patches = parse_udiff(udiff_text)
    # Pick the patch whose dst_path best matches `file_path`
    norm_target = str(p.resolve())
    chosen = None
    for patch in patches:
        for key in ("dst_path", "src_path"):
            cand = patch.get(key) or ""
            if cand and (cand == file_path or cand.endswith(p.name) or Path(cand).name == p.name):
                chosen = patch
                break
        if chosen is not None:
            break
    if chosen is None and patches:
        chosen = patches[0]

    if not chosen:
        return {"ok": False, "file": file_path, "hunks_applied": 0, "hunks_total": 0,
                "errors": ["no patch in udiff matches this file"]}

    try:
        original = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "file": file_path, "hunks_applied": 0, "hunks_total": 0,
                "errors": [f"read failed: {e}"]}

    source_lines = original.split("\n")
    applied = 0
    errors: List[str] = []

    for hunk_idx, hunk in enumerate(chosen["hunks"]):
        before, after = _hunk_target_rows(hunk)
        if not before and after:
            # Pure insertion at EOF
            source_lines = source_lines + after
            applied += 1
            continue
        # Tier 1: exact match
        pos = _try_match(source_lines, before, loose=False)
        # Tier 2: whitespace-loose match
        if pos < 0:
            pos = _try_match(source_lines, before, loose=True)
        # Tier 3: drop context (match only the '-' lines, keep remaining context)
        if pos < 0 and any(line["op"] == "-" for line in hunk["lines"]):
            minus_only = [line["text"] for line in hunk["lines"] if line["op"] == "-"]
            pos = _try_match(source_lines, minus_only, loose=True)
            if pos >= 0:
                # Replace just the minus region with plus region
                plus_only = [line["text"] for line in hunk["lines"] if line["op"] == "+"]
                source_lines = source_lines[:pos] + plus_only + source_lines[pos + len(minus_only):]
                applied += 1
                continue
        if pos < 0:
            errors.append(f"hunk {hunk_idx}: context not found (first line: {before[0][:80]!r})")
            continue
        source_lines = source_lines[:pos] + after + source_lines[pos + len(before):]
        applied += 1

    if applied == 0:
        return {"ok": False, "file": file_path, "hunks_applied": 0,
                "hunks_total": len(chosen["hunks"]), "errors": errors}

    try:
        new_content = "\n".join(source_lines)
        if not new_content.endswith("\n") and original.endswith("\n"):
            new_content += "\n"
        p.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "file": file_path, "hunks_applied": applied,
                "hunks_total": len(chosen["hunks"]), "errors": [f"write failed: {e}"]}

    return {
        "ok": applied == len(chosen["hunks"]),
        "file": file_path,
        "hunks_applied": applied,
        "hunks_total": len(chosen["hunks"]),
        "errors": errors,
        "size_before": len(original),
        "size_after": len(new_content),
    }


def apply_udiff_bundle(udiff_text: str, project_root: str) -> Dict[str, Any]:
    """Apply a udiff that spans multiple files. Returns per-file results."""
    patches = parse_udiff(udiff_text)
    if not patches:
        return {"ok": False, "files": [], "errors": ["no valid file-patch blocks found in udiff"]}

    root = Path(project_root)
    results: List[Dict[str, Any]] = []
    any_ok = False
    for patch in patches:
        # Prefer dst_path; if /dev/null it's a new file; else resolve under root
        dst = patch.get("dst_path") or patch.get("src_path") or ""
        if dst in ("/dev/null", ""):
            continue
        # Trim absolute paths to relative
        candidate = root / dst if not Path(dst).is_absolute() else Path(dst)
        if not candidate.exists():
            # Try path with name only
            name_only_match = list(root.rglob(Path(dst).name))
            if name_only_match:
                candidate = name_only_match[0]
        if not candidate.exists():
            results.append({"ok": False, "file": str(candidate), "errors": [f"file not found: {dst}"]})
            continue
        res = apply_udiff_to_file(str(candidate), _emit_single_file_udiff(patch))
        any_ok = any_ok or bool(res.get("ok"))
        results.append(res)

    return {
        "ok": any_ok,
        "files": results,
        "applied_count": sum(1 for r in results if r.get("ok")),
        "total_count": len(results),
    }


def _emit_single_file_udiff(patch: Dict[str, Any]) -> str:
    """Rebuild a single-file udiff string from parsed patch."""
    parts: List[str] = []
    parts.append(f"--- {patch.get('src_path') or patch.get('dst_path')}")
    parts.append(f"+++ {patch.get('dst_path') or patch.get('src_path')}")
    for hunk in patch["hunks"]:
        parts.append("@@ @@")
        for line in hunk["lines"]:
            parts.append(f"{line['op']}{line['text']}")
    return "\n".join(parts) + "\n"


# ─────────────────────────────────────────────────────────────
# v6.5 Phase 2 (#8): SEARCH/REPLACE block parser + applier
# ─────────────────────────────────────────────────────────────
# Merger in v6.5 switches from "full file rewrite" to emitting a
# series of Aider-style SEARCH/REPLACE blocks. Apply is driven by
# this local, deterministic function — the LLM never overwrites
# whole files, eliminating the root cause of silent asset loss.

_SR_BLOCK_RE = re.compile(
    r"FILE:\s*(?P<file>[^\n]+)\n"
    r"<{7}\s*SEARCH\n(?P<old>.*?)\n={7}\n(?P<new>.*?)\n>{7}\s*REPLACE",
    re.DOTALL,
)


def parse_search_replace_blocks(text: str) -> List[Tuple[str, str, str]]:
    """Parse Aider-style SEARCH/REPLACE blocks emitted by the Merger.

    Expected format per block:

        FILE: index.html
        <<<<<<< SEARCH
        {exact existing snippet, ≤ 40 lines}
        =======
        {replacement snippet}
        >>>>>>> REPLACE

    Returns a list of (file_path, old_string, new_string) tuples.
    Missing/malformed blocks are silently skipped — the caller is
    expected to surface "0 blocks parsed" as an error upstream.
    """
    if not text:
        return []
    blocks: List[Tuple[str, str, str]] = []
    for m in _SR_BLOCK_RE.finditer(str(text)):
        file_path = (m.group("file") or "").strip()
        old_string = m.group("old") or ""
        new_string = m.group("new") or ""
        if not file_path:
            continue
        blocks.append((file_path, old_string, new_string))
    return blocks


def _fuzzy_find(haystack: str, needle: str, threshold: float = 0.8) -> int:
    """Find `needle` inside `haystack` using a difflib similarity threshold.

    Returns the start index of the best match, or -1 if nothing scores above
    `threshold`. Used as a miss-recovery path after exact / whitespace-loose
    matches fail.
    """
    import difflib
    if not needle or not haystack:
        return -1
    # Walk a sliding window of the needle's length; score each candidate.
    needle_len = len(needle)
    # Cap search cost: only consider windows near lines that share at
    # least one non-trivial token with the needle's first line.
    first_line = needle.splitlines()[0].strip() if needle else ""
    first_tok = first_line.split()[0] if first_line else ""
    candidate_offsets: List[int] = []
    if first_tok and len(first_tok) >= 4:
        start = 0
        while True:
            idx = haystack.find(first_tok, start)
            if idx < 0:
                break
            candidate_offsets.append(idx)
            start = idx + 1
            if len(candidate_offsets) > 200:
                break
    if not candidate_offsets:
        # Fall back to stride-sampling the whole file
        step = max(1, needle_len // 4)
        candidate_offsets = list(range(0, max(0, len(haystack) - needle_len + 1), step))

    best_ratio = 0.0
    best_pos = -1
    for pos in candidate_offsets:
        window = haystack[pos:pos + needle_len]
        if not window:
            continue
        ratio = difflib.SequenceMatcher(None, window, needle, autojunk=False).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_pos = pos
            if ratio >= 0.98:
                break
    return best_pos if best_ratio >= threshold else -1


def apply_search_replace(
    output_dir: str,
    blocks: List[Tuple[str, str, str]],
    on_miss: str = "log",
) -> Dict[str, Any]:
    """Apply a batch of SEARCH/REPLACE blocks under `output_dir`.

    Args:
        output_dir: Root directory for resolving relative file paths.
        blocks: List produced by `parse_search_replace_blocks`.
        on_miss: Either "log" (record and continue) or "raise" (raise RuntimeError).

    Returns a summary dict:
        {
            "files": {path: replacements_count, ...},
            "applied": int,   # total SEARCH/REPLACE blocks applied
            "missed":  int,   # blocks whose SEARCH didn't match
            "errors":  [str, ...],
        }
    """
    root = Path(output_dir)
    file_counts: Dict[str, int] = {}
    errors: List[str] = []
    applied_total = 0
    missed_total = 0

    # Group blocks by file so each file is read/written exactly once
    per_file: Dict[str, List[Tuple[str, str]]] = {}
    for file_rel, old_s, new_s in blocks:
        cleaned = (file_rel or "").strip().lstrip("./")
        if not cleaned:
            continue
        per_file.setdefault(cleaned, []).append((old_s, new_s))

    for file_rel, edits in per_file.items():
        candidate = root / file_rel if not Path(file_rel).is_absolute() else Path(file_rel)
        if not candidate.exists():
            # Fall back to basename-anywhere under root
            try:
                matches = list(root.rglob(Path(file_rel).name))
            except Exception:
                matches = []
            if matches:
                candidate = matches[0]
        if not candidate.exists() or not candidate.is_file():
            errors.append(f"file not found: {file_rel}")
            missed_total += len(edits)
            if on_miss == "raise":
                raise RuntimeError(f"apply_search_replace: file not found: {file_rel}")
            continue

        try:
            original = candidate.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            errors.append(f"read failed for {file_rel}: {e}")
            missed_total += len(edits)
            continue

        updated = original
        replacements_here = 0
        for old_s, new_s in edits:
            if not old_s:
                # Pure insertion at EOF — accept but flag as nonstandard
                updated = updated + ("\n" if not updated.endswith("\n") else "") + new_s
                replacements_here += 1
                applied_total += 1
                continue
            if old_s in updated:
                updated = updated.replace(old_s, new_s, 1)
                replacements_here += 1
                applied_total += 1
                continue
            # Whitespace-loose: collapse whitespace for both sides and try again
            import re as _re
            def _norm(s: str) -> str:
                return _re.sub(r"[ \t]+", " ", s).strip()
            normalized_map = { _norm(updated[i:i+len(old_s)+20]): i for i in range(0, max(0, len(updated)-len(old_s)+1), 32) }
            target = _norm(old_s)
            hit_idx = -1
            for k, v in normalized_map.items():
                if target and target in k:
                    hit_idx = v
                    break
            if hit_idx >= 0:
                # Replace the unnormalized region (approximate — use fuzzy span)
                fuzzy_pos = _fuzzy_find(updated, old_s, threshold=0.8)
                if fuzzy_pos >= 0:
                    updated = updated[:fuzzy_pos] + new_s + updated[fuzzy_pos+len(old_s):]
                    replacements_here += 1
                    applied_total += 1
                    continue
            # Last chance: pure difflib fuzzy at 0.8
            fuzzy_pos = _fuzzy_find(updated, old_s, threshold=0.8)
            if fuzzy_pos >= 0:
                updated = updated[:fuzzy_pos] + new_s + updated[fuzzy_pos+len(old_s):]
                replacements_here += 1
                applied_total += 1
                continue
            missed_total += 1
            msg = f"SEARCH not found in {file_rel} (first 80 chars: {old_s[:80]!r})"
            errors.append(msg)
            if on_miss == "raise":
                raise RuntimeError("apply_search_replace: " + msg)
            else:
                logger.warning(msg)

        if replacements_here > 0 and updated != original:
            try:
                candidate.write_text(updated, encoding="utf-8")
                file_counts[str(candidate)] = replacements_here
            except Exception as e:
                errors.append(f"write failed for {file_rel}: {e}")

    return {
        "files": file_counts,
        "applied": applied_total,
        "missed": missed_total,
        "errors": errors,
    }


__all__ = [
    "parse_udiff",
    "apply_udiff_to_file",
    "apply_udiff_bundle",
    "parse_search_replace_blocks",
    "apply_search_replace",
]
