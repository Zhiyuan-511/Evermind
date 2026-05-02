"""
Evermind v7.1g — CLI Configuration Bootstrap

Runs once at server startup. Idempotently writes user-home config files that
unleash full CLI capability (skills, sub-agents, hooks, MCP servers, codex
profiles, output schemas). Designed for zero-touch first-run experience:
a new user installing Evermind.app gets all v7.1g optimizations automatically.

Idempotency rules:
- File missing → write it
- File present and our content is a strict subset → don't touch (user may have customized)
- File present and we need to add a key/section → merge non-destructively
- For TOML/JSON files we patch, always back up first to <name>.bak.evermind_bootstrap.<ts>

Logging is verbose so the user (or the maintainer) can audit exactly what got written.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("evermind.bootstrap")

# ─────────────────────────────────────────────
# Embedded resource templates (single source of truth)
# ─────────────────────────────────────────────

_MCP_SERVERS_DEFAULT: Dict[str, Dict[str, Any]] = {
    "playwright": {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
    },
    "fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
    },
    "memory": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
    },
    "sequentialthinking": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
    },
}

_CLAUDE_HOOK_SCRIPT = r"""#!/bin/bash
# Evermind v7.1g auto-format hook (auto-installed by bootstrap)
# Runs prettier/black on files written under /tmp/evermind_output/.
# Best-effort: never blocks the call, never fails the calling tool.

set +e
INPUT=$(cat 2>/dev/null)
[ -z "$INPUT" ] && exit 0

FILE=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    p = d.get('tool_input', {}).get('file_path') or d.get('tool_input', {}).get('path') or ''
    print(p, end='')
except Exception:
    pass
" 2>/dev/null)

case "$FILE" in
  /tmp/evermind_output/*) : ;;
  /private/tmp/evermind_output/*) : ;;
  *) exit 0 ;;
esac

[ -f "$FILE" ] || exit 0
SIZE=$(wc -c < "$FILE" 2>/dev/null | tr -d ' ')
[ "${SIZE:-0}" -gt 512000 ] && exit 0

case "$FILE" in
  *.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs|*.json|*.css|*.html|*.htm|*.scss|*.md)
    if command -v npx >/dev/null 2>&1; then
      perl -e 'alarm 5; exec @ARGV' npx --no-install prettier --write "$FILE" >/dev/null 2>&1 || true
    fi
    ;;
  *.py)
    if command -v black >/dev/null 2>&1; then
      perl -e 'alarm 5; exec @ARGV' black --quiet "$FILE" >/dev/null 2>&1 || true
    fi
    ;;
esac
exit 0
"""

_SKILLS: Dict[str, str] = {}
_AGENTS: Dict[str, str] = {}
_CODEX_SCHEMAS: Dict[str, Dict[str, Any]] = {}

_CODEX_PROFILES = """
# ========================================================================
# v7.1g Evermind profiles (auto-installed by bootstrap) — per-node-type
# reasoning effort tuning. DO NOT delete; orchestrator references these.
# ========================================================================
[profiles.evermind-planner]
model = "gpt-5.4"
model_reasoning_effort = "high"
model_provider = "gmn"
approval_policy = "never"
sandbox_mode = "workspace-write"

[profiles.evermind-builder]
model = "gpt-5.4"
model_reasoning_effort = "medium"
model_provider = "gmn"
approval_policy = "never"
sandbox_mode = "workspace-write"

[profiles.evermind-merger]
model = "gpt-5.4-mini"
model_reasoning_effort = "minimal"
model_provider = "gmn"
approval_policy = "never"
sandbox_mode = "workspace-write"

[profiles.evermind-polisher]
model = "gpt-5.4"
model_reasoning_effort = "low"
model_provider = "gmn"
approval_policy = "never"
sandbox_mode = "workspace-write"

[profiles.evermind-reviewer]
model = "gpt-5.4"
model_reasoning_effort = "high"
model_provider = "gmn"
approval_policy = "never"
sandbox_mode = "workspace-write"

[profiles.evermind-patcher]
model = "gpt-5.4"
model_reasoning_effort = "medium"
model_provider = "gmn"
approval_policy = "never"
sandbox_mode = "workspace-write"
"""

_CODEX_MCP_SERVERS = """
# v7.1g MCP server pool (auto-installed by bootstrap)
[mcp_servers.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest"]

[mcp_servers.fetch]
command = "uvx"
args = ["mcp-server-fetch"]

[mcp_servers.memory]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-memory"]

[mcp_servers.sequentialthinking]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-sequential-thinking"]
"""


def _read_skill_template(name: str) -> str:
    """Read skill markdown file from the bundled resources or return empty.
    The actual SKILL.md content is created at install time below.
    """
    return _SKILLS.get(name, "")


def _ts() -> int:
    return int(time.time() * 1000)


def _backup_then(path: Path) -> Path:
    """Back up `path` to `<path>.bak.evermind_bootstrap.<ts>` and return backup path.
    Caller is expected to overwrite `path` after this. No-op if path doesn't exist.
    """
    if not path.exists():
        return path
    bak = path.with_suffix(path.suffix + f".bak.evermind_bootstrap.{_ts()}")
    try:
        shutil.copy2(path, bak)
    except Exception as e:
        logger.warning("Backup of %s failed (continuing anyway): %s", path, e)
    return bak


def _patch_json_file(path: Path, top_level_patch: Dict[str, Any]) -> bool:
    """Merge top_level_patch into the JSON file at `path`. Idempotent — keys
    that already exist are NOT overwritten (user customizations preserved).
    Returns True if the file was modified.
    """
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    else:
        current = {}
    if not isinstance(current, dict):
        current = {}
    changed = False
    for k, v in top_level_patch.items():
        if k not in current:
            current[k] = v
            changed = True
        elif isinstance(current.get(k), dict) and isinstance(v, dict):
            for sub_k, sub_v in v.items():
                if sub_k not in current[k]:
                    current[k][sub_k] = sub_v
                    changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _backup_then(path)
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed


def _append_to_toml_if_missing(path: Path, sentinel: str, body: str) -> bool:
    """Append `body` to `path` if `sentinel` is not in the file. Returns True if appended."""
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except Exception:
            existing = ""
    if sentinel in existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _backup_then(path)
    with path.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(body)
    return True


def _write_file_if_missing(path: Path, content: str, executable: bool = False) -> bool:
    """Write `content` to `path` if it doesn't exist. Returns True if written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return True


# ─────────────────────────────────────────────
# Skill content templates
# ─────────────────────────────────────────────

_SKILLS["evermind-builder"] = """---
name: evermind-builder
description: Use when generating production-grade web/desktop project files in Evermind multi-agent pipeline. Activates for any task that creates HTML/CSS/JS/TS/Python source code as part of a builder lane.
allow_implicit_invocation: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch, TodoWrite
---

# Evermind Builder Skill

You are a builder lane in a multi-agent pipeline. Your output feeds merger → polisher → reviewer → tester. Every line of code you write must:

## Quality bar
1. **No placeholders** — zero `TODO`, `FIXME`, `lorem ipsum`, `[image]`, `placeholder`, `XXX`, empty href, empty src.
2. **Real copy** — every visible string is content, not "Some heading here".
3. **Inline SVG only** for UI icons (24×24, never emoji glyphs).
4. **CSS custom properties** for every color (no inline hex).
5. **System font stack** (no remote font CDNs).
6. **Production accessibility** — semantic HTML, lang attribute, aria labels, focus-visible.
7. **Responsive** — fluid clamp() typography + container queries (NOT viewport media queries when @container available).
8. **Core Web Vitals** — explicit width/height on `<img>`, fetchpriority on hero, content-visibility on below-fold sections.

## Cross-node contract
Read the analyst's `<cross_node_contract>` block FIRST. Match dom_ids, function names, CSS custom props, and copy_hooks character-for-character. The reviewer + tester will verify these.

## Cross-CLI pairing contract
If analyst emitted `<cli_pairing_contract>`, you must:
- Match `api_base_url` + `endpoints` exactly (request_schema/response_schema/error_codes)
- Use `localStorage_keys` verbatim
- Import `shared_design_token_file` instead of redefining tokens
- Honor `cli_specialization` for your lane

## Accuracy-critical imagery
If analyst emitted `<accuracy_critical_imagery enabled: true>`:
1. **DO NOT invent illustrations of real-world subjects** (sign language, anatomy, flags, species, landmarks). LLM-drawn versions are wrong.
2. WebFetch every URL from `asset_manifest`, save to `download_destination`.
3. After saving each file, Read first 128 bytes — verify it's a real SVG/PNG (not a 404 HTML page).
4. Use real `<img src="..." alt="<content_hint>">` — alt text from analyst's content_hint, not generic.
5. Emit `<image_failures>` for any failed item; do NOT silently substitute.

## Final-message protocol
Your FINAL assistant message is the orchestrator's primary capture. Include the full deliverable inline; tool work that comes before is invisible. Do NOT say "I created the files" — list them with their purpose.

## Dual-delivery
For text deliverables >5KB: also write to `/tmp/evermind_output/handoff/<lane>_report.xml`. Orchestrator falls back to that file if final message truncates.

## Forbidden
- `float:*` for layout
- `position:absolute` except tooltips/modals/sticky CTAs
- `<br><br>` as spacing
- `localStorage`/`sessionStorage` unless task explicitly requires persistence
- `console.log` in production output
- `alert()`/`confirm()`/`prompt()`
- Remote font CDNs (`fonts.googleapis.com`, `typekit.net`)
- Emoji glyphs as icons or content

## Repair-pass rule
Round 2+ uses `Edit` with smallest possible old_string→new_string pairs. Returning a full `<!DOCTYPE>...</html>` rewrite on round 2+ is FORBIDDEN — orchestrator's post_write_full_rewrite_loop guard cuts the stream.
"""

_SKILLS["evermind-merger"] = """---
name: evermind-merger
description: Use when integrating multiple builder lane outputs into a unified codebase. 4-way merge: dedup duplicate files, resolve token conflicts, ensure design-token consistency.
allow_implicit_invocation: true
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Evermind Merger Skill

Your job: integrate 4 parallel builder lanes (builder1-4) into one coherent project.

## Strategy
1. **Inventory first** — list every file written by every lane. Map ownership.
2. **NOOP when safe** — if a file appears in only one lane, just copy it. Don't LLM-merge what doesn't need merging.
3. **Three-way merge for shared files** — when 2+ lanes touched the same file, identify the analyst's intended ownership and prefer that lane's version, then patch in non-conflicting additions from others.
4. **Design tokens are the single source of truth** — if any lane redefined `:root { --color-* }`, replace with `@import "shared/design-tokens.css"`.
5. **Smoke-validate** — every page links resolve (no `<script src="missing.js">`), every CSS custom prop used has a definition.

## Hard rules
- DO NOT rewrite shared files from scratch. Surgical edits only.
- DO NOT introduce new logic — you are integrating, not building.
- Final output structure must match analyst's `deliverables_contract` directory layout.

## Output
Final message: list of files in final project + 1-line summary per integration decision (which lane's version won). NO narration of the merge process.
"""

_SKILLS["evermind-reviewer"] = """---
name: evermind-reviewer
description: Use when conducting strict commercial-grade quality gate review of generated artifacts. Browser-driven evidence-based audit; not stylistic critique.
allow_implicit_invocation: true
allowed-tools: Read, Glob, Grep, WebFetch, Bash
---

# Evermind Reviewer Skill

You are a strict QA gate. REJECT bias is correct — only ship if commercial-ready.

## Evidence-driven (not opinion-based)
Every finding must reference a specific file/line/observation. "Looks bad" is not a finding. "index.html:142 hardcodes #5A4A3A bypassing --color-text token" is a finding.

## Verify against analyst's contracts
1. **cross_node_contract** — every dom_id, function name, css custom prop the analyst listed must exist. grep for each.
2. **cli_pairing_contract** — frontend's fetch URLs must match backend's route definitions. Verify by reading both sides.
3. **accuracy_critical_imagery** — open each `asset_manifest` item's local file AND source URL; confirm content_hint matches what's actually rendered. For sign-language: ensure letter A's image actually shows letter A.

## Browser audit
Navigate to preview/index.html. Verify:
- Page loads without console errors (tolerance: ≤3 non-critical)
- Hero region renders within first viewport
- Primary CTA is clickable and triggers expected behavior
- Scroll to bottom — no broken layout, no overlap
- Tab order is logical
- All routes return 200 (not 404)
- All linked CSS/JS resolve (no FOUT/FOUC from missing styles)

## Rejection categories (any one = REJECT)
- Console errors > 3
- Missing routes from deliverables_contract
- Hardcoded inline colors bypassing tokens
- Emoji glyphs in content (not just decorative)
- Placeholder strings (TODO/lorem ipsum/[image])
- Accuracy-critical images failed verification
- Reviewer cannot reach 1 working interaction

## Output
Strict JSON:
```
{"verdict": "APPROVED" | "REJECTED",
 "scores": {"layout":N, "color":N, "typography":N, "interaction":N, "responsive":N, "completeness":N, "accuracy":N},
 "blocking_issues": [...],
 "required_changes": [...],
 "evidence_anchors": [...]}
```
"""

_SKILLS["accuracy-critical-imagery"] = """---
name: accuracy-critical-imagery
description: Use when the project requires REAL authoritative images that must 1-to-1 match labels (sign language, anatomy, flags, species ID, landmarks, official logos, music notation, math formulas). LLM-invented illustrations are forbidden in this mode.
allow_implicit_invocation: true
allowed-tools: WebFetch, WebSearch, Read, Write, Bash
---

# Accuracy-Critical Imagery Protocol

When invoked, the project demands authoritative imagery — invented SVG/PNG by an LLM will be wrong (you don't actually know exact ASL handshapes, organ anatomy, or flag heraldry).

## Source priority
1. **upload.wikimedia.org/wikipedia/commons/<hash>/<file>** — canonical, PD/CC, raw asset URL
2. **commons.wikimedia.org/wiki/Category:<topic>** — discover hash URLs from category pages
3. **en.wikipedia.org/wiki/<topic>** — article tables often have authoritative image references
4. **NIH/government domains** — for medical/anatomy
5. **CIA World Factbook** — for flag/geographic

## Procedure
For each item in the manifest:

1. **Fetch the canonical URL via WebFetch.**
   - Verify HTTP 200, content-type matches (image/svg+xml, image/png, image/jpeg)
   - Read first 128 bytes — confirm magic bytes (`<svg`, `\\x89PNG`, `\\xff\\xd8\\xff`)
   - If it's HTML 404 wrapper, mark as `fetch_failed`, do NOT save

2. **Save to `<workspace>/public/assets/<domain>/<id>.<ext>`** with id from manifest.

3. **Cross-check content_hint:**
   - For SVGs: grep the file for `<title>` / `<desc>` matching the hint
   - For raster images: verify dimensions are sensible (not 1×1 placeholder)
   - If ANY mismatch, mark `wrong_content`, do NOT use

4. **Bind to UI:**
   - `<img src="assets/<domain>/<id>.<ext>" alt="<content_hint>" loading="lazy" width="..." height="...">`
   - alt text from content_hint, NOT generic "letter A"
   - Source attribution per CC requirements (footer link to original URL)

## Failure modes
- Silent placeholder substitution = FORBIDDEN. Always emit `<image_failures>` block.
- Skipping verification because URL "looks valid" = FORBIDDEN.
- Embedding base64 of large images inline = avoid (>50KB hurts paint).
"""

# ─────────────────────────────────────────────
# Sub-agent content templates
# ─────────────────────────────────────────────

_AGENTS["codebase-researcher"] = """---
name: codebase-researcher
description: Fast codebase exploration and pattern discovery. Dispatch when builder/merger/reviewer/debugger needs to understand existing code structure or find related files. READ-ONLY — no modifications.
tools: Read, Glob, Grep
model: haiku
---

You are a fast codebase researcher. Use Haiku-fast scanning to map structure efficiently.

## Mission
When dispatched, your job is to find and summarize relevant code without loading massive context into the parent agent. Return a TIGHT report — not a paste of file contents.

## Procedure
1. **Glob first** to find relevant files (`**/*.{ts,tsx,js,jsx,py,html,css}`).
2. **Grep for patterns** the parent asked about (function names, route paths, CSS class names, design tokens).
3. **Read selectively** — only files that actually contain matching content. Quote 5-15 line snippets, not whole files.
4. **Return a structured summary**:
   - File inventory (paths grouped by purpose)
   - Key patterns + 1-line description each
   - Naming conventions observed
   - Direct dependencies / imports
   - Missing pieces / gaps

## Output bar
- Max 2000 chars unless caller requested deeper dive
- Quote real code (with file:line citations), not paraphrase
- Flag risks: hardcoded values, anti-patterns, dead code

## Don't
- Don't modify any file
- Don't run shell commands beyond Glob/Grep/Read
- Don't summarize files that don't match the query
"""

_AGENTS["image-fetcher"] = """---
name: image-fetcher
description: Use when the task requires downloading authoritative images from web sources (Wikipedia/Wikimedia Commons/NIH/CIA Factbook). Verifies content match against expected hint, saves locally with attribution. ASL/anatomy/flags/landmarks specialty.
tools: WebFetch, WebSearch, Read, Write, Bash
model: sonnet
---

You are an image fetcher specialized in **accuracy-critical** imagery. Your job is to download REAL authoritative images and verify they match what the caller expects.

## Trigger contexts
- Sign language alphabet (e.g. ASL letter A → Wikimedia Sign_language_A.svg)
- Anatomy diagrams (organs, bones)
- World flags (must match country exactly)
- Species ID (must match common name + Latin binomial)
- Landmarks / official logos / music notation / math formulas

## Procedure per item
1. **Search**: WebSearch for `<topic> <subtype> wikimedia commons site:upload.wikimedia.org`
2. **Fetch candidate URL** via WebFetch. Verify HTTP 200 + content-type matches expected (image/svg+xml, image/png, image/jpeg).
3. **Download** to caller-specified path via curl or Bash equivalent.
4. **Verify** first 128 bytes — must be magic bytes for the format, NOT an HTML 404 wrapper.
5. **Cross-check content_hint**:
   - SVG: grep file for `<title>` / `<desc>` matching the hint
   - Raster: dimensions sensible (>32px each side)
6. Return verdict: `verified_match` | `wrong_content` | `fetch_failed`

## Forbidden
- Inventing SVGs / hand-drawing illustrations of real subjects
- Silent placeholder substitution
- Skipping verification because URL "looks valid"
- Using non-PD/non-CC sources without attribution plan

## Output structure
```json
{"manifest_results": [
  {"id":"A","source_url":"...","saved_to":"...","verification":"verified_match","attribution":"Wikipedia · public domain"},
  {"id":"B","source_url":"...","saved_to":"","verification":"fetch_failed","reason":"404 on canonical URL"}
]}
```
"""

# ─────────────────────────────────────────────
# Codex output schema templates
# ─────────────────────────────────────────────

_CODEX_SCHEMAS["builder"] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Evermind Codex Builder Output",
    "type": "object",
    "required": ["files_written", "summary", "lane_id"],
    "additionalProperties": False,
    "properties": {
        "lane_id": {"type": "string"},
        "files_written": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "bytes", "purpose"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "bytes": {"type": "integer", "minimum": 1},
                    "purpose": {"type": "string", "maxLength": 200},
                },
            },
        },
        "files_modified": {"type": "array", "items": {"type": "object"}},
        "external_assets_fetched": {"type": "array", "items": {"type": "object"}},
        "summary": {"type": "string", "maxLength": 800},
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
    },
}

_CODEX_SCHEMAS["merger"] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Evermind Codex Merger Output",
    "type": "object",
    "required": ["lanes_merged", "final_files", "summary"],
    "additionalProperties": False,
    "properties": {
        "lanes_merged": {"type": "array", "items": {"type": "string"}},
        "final_files": {"type": "array", "items": {"type": "object"}},
        "conflicts_resolved": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "maxLength": 600},
    },
}

_CODEX_SCHEMAS["patcher"] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Evermind Codex Patcher Output",
    "type": "object",
    "required": ["fixes_applied", "summary"],
    "additionalProperties": False,
    "properties": {
        "fixes_applied": {"type": "array", "items": {"type": "object"}},
        "issues_remaining": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string", "maxLength": 500},
    },
}


# ─────────────────────────────────────────────
# Bootstrap entry point
# ─────────────────────────────────────────────

def bootstrap_user_configs(home: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
    """Run all bootstrap steps. Idempotent.

    Args:
        home: override the user home dir (default: Path.home()).
        force: when True, overwrite existing files (DESTRUCTIVE — backs up first).

    Returns a report dict listing what was written, skipped, or backed up.
    """
    home = home or Path.home()
    report: Dict[str, Any] = {
        "created": [],
        "skipped_existing": [],
        "patched": [],
        "backed_up": [],
        "errors": [],
    }

    def _safe(fn, label, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            report["errors"].append({"step": label, "error": str(e)[:300]})
            logger.warning("[bootstrap] %s failed: %s", label, e)
            return None

    # 1) Codex schemas
    schemas_dir = home / ".openclaw" / "workspace" / "codex_schemas"
    for name, schema in _CODEX_SCHEMAS.items():
        path = schemas_dir / f"{name}.json"
        if path.exists() and not force:
            report["skipped_existing"].append(str(path))
            continue
        _safe(
            lambda p=path, s=schema: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(json.dumps(s, indent=2), encoding="utf-8")),
            f"codex_schema:{name}",
        )
        report["created"].append(str(path))

    # 2) Claude skills
    skills_root = home / ".claude" / "skills"
    for name, body in _SKILLS.items():
        skill_dir = skills_root / name
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists() and not force:
            report["skipped_existing"].append(str(skill_md))
            continue
        _safe(
            lambda p=skill_md, b=body: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(b, encoding="utf-8")),
            f"claude_skill:{name}",
        )
        report["created"].append(str(skill_md))

    # 3) Claude sub-agents
    agents_root = home / ".claude" / "agents"
    for name, body in _AGENTS.items():
        agent_md = agents_root / f"{name}.md"
        if agent_md.exists() and not force:
            report["skipped_existing"].append(str(agent_md))
            continue
        _safe(
            lambda p=agent_md, b=body: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(b, encoding="utf-8")),
            f"claude_agent:{name}",
        )
        report["created"].append(str(agent_md))

    # 4) Claude auto-format hook script
    hook_path = home / ".claude" / "hooks" / "auto-format-evermind.sh"
    if hook_path.exists() and not force:
        report["skipped_existing"].append(str(hook_path))
    else:
        if _safe(lambda: _write_file_if_missing(hook_path, _CLAUDE_HOOK_SCRIPT, executable=True), "claude_hook_script"):
            report["created"].append(str(hook_path))
        try:
            hook_path.chmod(0o755)
        except Exception:
            pass

    # 5) Claude .mcp.json (project-level for ~/.claude itself)
    claude_mcp_path = home / ".claude" / ".mcp.json"
    if claude_mcp_path.exists() and not force:
        # Merge missing servers
        try:
            current = json.loads(claude_mcp_path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
        current.setdefault("mcpServers", {})
        added = False
        for k, v in _MCP_SERVERS_DEFAULT.items():
            if k not in current["mcpServers"]:
                current["mcpServers"][k] = v
                added = True
        if added:
            _backup_then(claude_mcp_path)
            claude_mcp_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
            report["patched"].append(str(claude_mcp_path))
        else:
            report["skipped_existing"].append(str(claude_mcp_path))
    else:
        body = {"mcpServers": _MCP_SERVERS_DEFAULT.copy()}
        _safe(
            lambda: (claude_mcp_path.parent.mkdir(parents=True, exist_ok=True),
                     claude_mcp_path.write_text(json.dumps(body, indent=2), encoding="utf-8")),
            "claude_mcp_json",
        )
        report["created"].append(str(claude_mcp_path))

    # 6) Claude settings.json — patch hooks block
    settings_path = home / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            current_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            current_settings = {}
    else:
        current_settings = {}
    if not isinstance(current_settings, dict):
        current_settings = {}
    hooks_section = current_settings.setdefault("hooks", {})
    post_use = hooks_section.setdefault("PostToolUse", [])
    has_evermind_hook = any(
        isinstance(h, dict) and "auto-format-evermind.sh" in str(h)
        for h in post_use
    )
    if not has_evermind_hook:
        post_use.append({
            "matcher": "Write|Edit",
            "hooks": [{
                "type": "command",
                "command": str(hook_path),
            }],
        })
        if settings_path.exists():
            _backup_then(settings_path)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(current_settings, indent=2), encoding="utf-8")
        report["patched"].append(str(settings_path))
    else:
        report["skipped_existing"].append(str(settings_path))

    # 7) Gemini settings.json — merge MCP + context.fileName + DNS hint
    gemini_settings_path = home / ".gemini" / "settings.json"
    if gemini_settings_path.exists():
        try:
            gemini_cfg = json.loads(gemini_settings_path.read_text(encoding="utf-8"))
        except Exception:
            gemini_cfg = {}
    else:
        gemini_cfg = {}
    changed_gemini = False
    gemini_cfg.setdefault("mcpServers", {})
    for k, v in _MCP_SERVERS_DEFAULT.items():
        if k not in gemini_cfg["mcpServers"]:
            gemini_cfg["mcpServers"][k] = v
            changed_gemini = True
    if "context" not in gemini_cfg:
        gemini_cfg["context"] = {"fileName": ["GEMINI.md", "AGENTS.md", "CLAUDE.md"]}
        changed_gemini = True
    elif "fileName" not in gemini_cfg["context"]:
        gemini_cfg["context"]["fileName"] = ["GEMINI.md", "AGENTS.md", "CLAUDE.md"]
        changed_gemini = True
    if "fileFiltering" not in gemini_cfg:
        gemini_cfg["fileFiltering"] = {
            "respectGitIgnore": True,
            "respectGeminiIgnore": True,
            "enableRecursiveFileSearch": False,
        }
        changed_gemini = True
    if "advanced" not in gemini_cfg:
        gemini_cfg["advanced"] = {"dnsResolutionOrder": "ipv4first"}
        changed_gemini = True
    if "summarizeToolOutput" not in gemini_cfg:
        gemini_cfg["summarizeToolOutput"] = True
        changed_gemini = True
    if changed_gemini:
        if gemini_settings_path.exists():
            _backup_then(gemini_settings_path)
        gemini_settings_path.parent.mkdir(parents=True, exist_ok=True)
        gemini_settings_path.write_text(json.dumps(gemini_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        report["patched"].append(str(gemini_settings_path))
    else:
        report["skipped_existing"].append(str(gemini_settings_path))

    # 8) Codex config.toml — append profiles + MCP servers
    codex_cfg_path = home / ".codex" / "config.toml"
    profile_added = _safe(
        lambda: _append_to_toml_if_missing(
            codex_cfg_path,
            sentinel="[profiles.evermind-builder]",
            body=_CODEX_PROFILES,
        ),
        "codex_profiles",
    )
    mcp_added = _safe(
        lambda: _append_to_toml_if_missing(
            codex_cfg_path,
            sentinel="[mcp_servers.playwright]",
            body=_CODEX_MCP_SERVERS,
        ),
        "codex_mcp_servers",
    )
    if profile_added or mcp_added:
        report["patched"].append(str(codex_cfg_path))

    return report


def bootstrap_at_startup() -> None:
    """Idempotent startup hook. Called from server.py once per process start."""
    try:
        report = bootstrap_user_configs(force=False)
        n_created = len(report["created"])
        n_patched = len(report["patched"])
        n_skipped = len(report["skipped_existing"])
        n_errors = len(report["errors"])
        logger.info(
            "[v7.1g bootstrap] created=%d patched=%d skipped=%d errors=%d",
            n_created, n_patched, n_skipped, n_errors,
        )
        if n_created or n_patched:
            logger.info("[v7.1g bootstrap] new files: %s", report["created"][:8])
            logger.info("[v7.1g bootstrap] patched files: %s", report["patched"][:8])
        if n_errors:
            logger.warning("[v7.1g bootstrap] errors: %s", report["errors"][:5])
    except Exception as e:
        logger.warning("[v7.1g bootstrap] aborted: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    rep = bootstrap_user_configs(force=False)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
