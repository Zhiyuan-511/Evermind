"""
Lightweight Aider-style repo map generation for existing-repository edit tasks.

The goal is not perfect indexing; it is to give builder/debugger enough stable
codebase context to choose the right files before editing.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

MANIFEST_NAMES = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "setup.py",
    "manage.py",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "vite.config.js",
    "vite.config.ts",
    "tsconfig.json",
)

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    ".next",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "tmp",
    "temp",
}

LANGUAGE_BY_SUFFIX = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".json": "JSON",
    ".css": "CSS",
    ".scss": "SCSS",
    ".html": "HTML",
    ".md": "Markdown",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".rb": "Ruby",
    ".php": "PHP",
    ".sh": "Shell",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
}

ENTRYPOINT_PRIORITY = (
    "server.py",
    "main.py",
    "app.py",
    "manage.py",
    "src/main.ts",
    "src/main.tsx",
    "src/index.ts",
    "src/index.tsx",
    "src/app/page.tsx",
    "src/app/layout.tsx",
    "pages/index.tsx",
    "pages/index.jsx",
    "index.ts",
    "index.js",
    "main.go",
    "Main.java",
)

EDIT_INTENT_RE = re.compile(
    r"(修复|排查|修改|改动|重构|优化|补丁|补全|接入|集成|适配|升级|迁移|更新|替换|删掉|删除|重命名|"
    r"fix|debug|patch|modify|edit|update|refactor|repair|integrate|wire|upgrade|migrate|rename|remove)",
    re.IGNORECASE,
)
REPO_HINT_RE = re.compile(
    r"(仓库|代码库|codebase|repo|repository|workspace|existing repo|existing code|this repo|当前仓库|"
    r"当前代码|现有代码|已有代码|当前项目代码|现有项目代码|package\.json|pyproject\.toml|requirements\.txt|"
    r"cargo\.toml|go\.mod|tsconfig\.json|next\.config|vite\.config|测试失败|编译失败|lint 失败|build failed|"
    r"failing test|stack trace|traceback|ci)",
    re.IGNORECASE,
)
FILE_HINT_RE = re.compile(
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+|"
    r"\b[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|swift|rb|php|json|yml|yaml|toml|md|css|scss)\b)",
    re.IGNORECASE,
)
EXPLICIT_REPO_CONTEXT_RE = re.compile(
    r"(仓库|代码库|codebase|repo|repository|workspace|existing repo|existing code|this repo|"
    r"当前仓库|当前代码|现有代码|已有代码|现有项目代码)",
    re.IGNORECASE,
)
GREENFIELD_OUTPUT_RE = re.compile(
    r"(file_ops|/tmp/evermind_output|index\.html plus|linked html page|multi-page website|"
    r"real html page under|save final html via file_ops write)",
    re.IGNORECASE,
)
GREENFIELD_BUILDER_CREATION_RE = re.compile(
    r"("
    r"(?:build|create|make|generate|develop)\b.*?(?:html5\s+game|game|website|site|landing(?:\s+page)?|homepage|dashboard|tool|app)"
    r"|(?:做一个|做个|创建一个|创建|生成一个|开发一个).{0,80}(?:网页游戏|游戏|网站|官网|页面|应用|工具|仪表盘)"
    r")",
    re.IGNORECASE | re.DOTALL,
)

_GENERIC_PREVIEW_FILE_HINTS = {
    "index.html",
    "about.html",
    "contact.html",
    "pricing.html",
    "styles.css",
    "style.css",
    "app.js",
    "main.js",
    "script.js",
}


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except Exception:
        return path.as_posix()


def _looks_like_repo_root(root: Path) -> bool:
    if not root.is_dir():
        return False
    if (root / ".git").exists():
        return True
    manifest_hits = sum(1 for name in MANIFEST_NAMES if (root / name).exists())
    if manifest_hits >= 1:
        return True
    common_source_dirs = ("src", "app", "lib", "backend", "frontend", "tests")
    if any((root / name).exists() for name in common_source_dirs):
        code_files = sum(1 for child in root.iterdir() if child.is_file() and child.suffix in LANGUAGE_BY_SUFFIX)
        return code_files >= 1 or any((root / name).is_dir() for name in common_source_dirs)
    return False


def _prompt_tokens(prompt_source: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9._-]{2,}", str(prompt_source or "").lower())
    deduped: List[str] = []
    for token in tokens:
        if token in {"fix", "bug", "repo", "codebase", "project", "build", "test", "debug", "update"}:
            deduped.append(token)
            continue
        if token not in deduped:
            deduped.append(token)
    return deduped[:24]


def _candidate_roots(workspace: Path) -> List[Path]:
    if not workspace.exists() or not workspace.is_dir():
        return []

    candidates: List[Path] = []
    seen: set[str] = set()

    def add_candidate(path: Path) -> None:
        try:
            resolved = str(path.resolve())
        except Exception:
            resolved = str(path)
        if resolved in seen:
            return
        if _looks_like_repo_root(path):
            seen.add(resolved)
            candidates.append(path)

    add_candidate(workspace)
    if candidates:
        return candidates

    queue: List[Tuple[Path, int]] = [(workspace, 0)]
    visited_dirs = 0
    while queue and visited_dirs < 80 and len(candidates) < 24:
        current, depth = queue.pop(0)
        if depth >= 2:
            continue
        try:
            children = sorted(
                [child for child in current.iterdir() if child.is_dir() and child.name not in IGNORED_DIRS],
                key=lambda item: item.name.lower(),
            )
        except Exception:
            continue
        for child in children[:30]:
            visited_dirs += 1
            add_candidate(child)
            if depth + 1 < 2:
                queue.append((child, depth + 1))
    return candidates


def _score_candidate(root: Path, prompt_tokens: Iterable[str]) -> int:
    score = 0
    root_name = root.name.lower()
    if (root / ".git").exists():
        score += 6
    manifest_count = sum(1 for name in MANIFEST_NAMES if (root / name).exists())
    score += manifest_count * 2
    if root_name in {"frontend", "backend", "app", "server", "web"}:
        score += 1
    for token in prompt_tokens:
        if token == root_name:
            score += 10
        elif token in root_name:
            score += 4
    return score


def resolve_repo_root(workspace: str, prompt_source: str = "") -> Optional[Path]:
    workspace_value = str(workspace or "").strip()
    if not workspace_value:
        return None
    try:
        workspace_path = Path(workspace_value).expanduser().resolve()
    except Exception:
        return None
    candidates = _candidate_roots(workspace_path)
    if not candidates:
        return None
    prompt_tokens = _prompt_tokens(prompt_source)
    ranked = sorted(candidates, key=lambda item: (_score_candidate(item, prompt_tokens), len(item.as_posix())), reverse=True)
    return ranked[0] if ranked else None


def is_repo_edit_task(prompt_source: str, workspace: str) -> bool:
    prompt = str(prompt_source or "").strip()
    if not prompt:
        return False
    if resolve_repo_root(workspace, prompt) is None:
        return False
    has_edit_intent = bool(EDIT_INTENT_RE.search(prompt))
    has_repo_hint = bool(REPO_HINT_RE.search(prompt))
    has_file_hint = bool(_has_explicit_repo_file_hint(prompt))
    has_failure_hint = bool(re.search(r"(报错|错误|bug|error|traceback|stack trace|failing test|build failed|lint failed)", prompt, re.IGNORECASE))
    return bool(
        (has_edit_intent and (has_repo_hint or has_file_hint))
        or (has_repo_hint and has_failure_hint)
    )


def _has_explicit_repo_file_hint(prompt_source: str) -> bool:
    prompt = str(prompt_source or "")
    for raw_hint in FILE_HINT_RE.findall(prompt):
        hint = str(raw_hint or "").strip().lower().rstrip(".,:;!?)]}\"'")
        if not hint:
            continue
        if (
            hint.startswith("/tmp/evermind_output")
            or hint.startswith("tmp/evermind_output")
            or "/tmp/evermind_output/" in hint
            or "tmp/evermind_output/" in hint
        ):
            continue
        basename = Path(hint).name.lower()
        suffix = Path(hint).suffix.lower()
        if hint in _GENERIC_PREVIEW_FILE_HINTS or basename in _GENERIC_PREVIEW_FILE_HINTS:
            continue
        if "/" in hint:
            if basename in {name.lower() for name in MANIFEST_NAMES}:
                return True
            if suffix:
                return True
            continue
        if suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".json", ".yml", ".yaml", ".toml", ".md"}:
            return True
    return False


def _looks_like_greenfield_builder_task(prompt_source: str) -> bool:
    prompt = str(prompt_source or "").strip()
    if not prompt:
        return False
    if EXPLICIT_REPO_CONTEXT_RE.search(prompt) or _has_explicit_repo_file_hint(prompt):
        return False
    if GREENFIELD_OUTPUT_RE.search(prompt):
        return True
    return bool(GREENFIELD_BUILDER_CREATION_RE.search(prompt))


def _load_package_scripts(path: Path) -> List[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return []
    return [str(key).strip() for key in scripts.keys() if str(key).strip()][:8]


def _interesting_path_score(rel_path: str) -> int:
    path = rel_path.lower()
    basename = Path(path).name
    score = 0
    if basename in MANIFEST_NAMES:
        score += 10
    if basename in {"server.py", "main.py", "app.py", "package.json", "tsconfig.json", "next.config.ts", "next.config.js"}:
        score += 10
    if "/tests/" in f"/{path}" or basename.startswith("test_") or ".test." in basename or ".spec." in basename:
        score += 6
    if any(path == candidate or path.endswith(f"/{candidate}") for candidate in ENTRYPOINT_PRIORITY):
        score += 8
    if any(fragment in path for fragment in ("/src/", "/app/", "/backend/", "/frontend/", "/components/", "/hooks/", "/plugins/")):
        score += 3
    if basename.startswith("."):
        score -= 4
    return score


def _iter_repo_files(root: Path, max_files: int = 1800) -> List[str]:
    files: List[str] = []
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIRS]
        current_path = Path(current_root)
        for file_name in sorted(file_names):
            if len(files) >= max_files:
                return files
            path = current_path / file_name
            files.append(_safe_rel(path, root))
    return files


def _render_tree_slice(files: List[str]) -> List[str]:
    grouped: Dict[str, List[str]] = {}
    for rel_path in files:
        parts = rel_path.split("/")
        if len(parts) == 1:
            grouped.setdefault(".", []).append(parts[0])
            continue
        grouped.setdefault(parts[0], []).append("/".join(parts[1:]))

    lines: List[str] = []
    for key in sorted(grouped.keys())[:8]:
        if key == ".":
            for item in sorted(grouped[key])[:5]:
                lines.append(f"- {item}")
            continue
        lines.append(f"- {key}/")
        children = sorted(grouped[key], key=lambda item: (_interesting_path_score(f"{key}/{item}"), -len(item)), reverse=True)
        for child in children[:4]:
            first = child.split("/")[0]
            if "/" in child:
                lines.append(f"  - {key}/{first}/")
            else:
                lines.append(f"  - {key}/{child}")
    return lines[:18]


@lru_cache(maxsize=16)
def _repo_snapshot(root_str: str) -> Dict[str, object]:
    root = Path(root_str)
    files = _iter_repo_files(root)
    languages: Dict[str, int] = {}
    manifests: List[str] = []
    tests: List[str] = []
    entrypoints: List[str] = []

    for rel_path in files:
        path_obj = Path(rel_path)
        suffix = path_obj.suffix.lower()
        label = LANGUAGE_BY_SUFFIX.get(suffix)
        if label:
            languages[label] = languages.get(label, 0) + 1
        if path_obj.name in MANIFEST_NAMES:
            manifests.append(rel_path)
        if (
            "/tests/" in f"/{rel_path}"
            or path_obj.name.startswith("test_")
            or ".test." in path_obj.name
            or ".spec." in path_obj.name
        ):
            tests.append(rel_path)
        if any(rel_path == item or rel_path.endswith(f"/{item}") for item in ENTRYPOINT_PRIORITY):
            entrypoints.append(rel_path)

    interesting = sorted(files, key=lambda item: (_interesting_path_score(item), item.count("/"), -len(item)), reverse=True)
    key_files = interesting[:12]
    tree_slice = _render_tree_slice(interesting[:120])

    package_manifests = [item for item in manifests if Path(item).name == "package.json"][:3]
    package_scripts: Dict[str, List[str]] = {}
    for manifest in package_manifests:
        scripts = _load_package_scripts(root / manifest)
        if scripts:
            package_scripts[manifest] = scripts

    verification_commands: List[str] = []
    for manifest, scripts in package_scripts.items():
        folder = Path(manifest).parent.as_posix()
        prefix = "" if folder == "." else f"{folder}: "
        if "build" in scripts:
            verification_commands.append(f"{prefix}npm run build")
        if "test" in scripts:
            verification_commands.append(f"{prefix}npm test")
        if "lint" in scripts:
            verification_commands.append(f"{prefix}npm run lint")
    if any(Path(item).name in {"pyproject.toml", "requirements.txt", "setup.py"} for item in manifests) or tests:
        verification_commands.append("repo root: pytest")
    if any(Path(item).name == "Cargo.toml" for item in manifests):
        verification_commands.append("repo root: cargo test")
    if any(Path(item).name == "go.mod" for item in manifests):
        verification_commands.append("repo root: go test ./...")

    return {
        "files": files,
        "languages": sorted(languages.items(), key=lambda item: item[1], reverse=True)[:6],
        "manifests": manifests[:8],
        "tests": tests[:8],
        "entrypoints": entrypoints[:8],
        "key_files": key_files,
        "tree_slice": tree_slice,
        "package_scripts": package_scripts,
        "verification_commands": verification_commands[:6],
    }


def build_repo_context(node_type: str, prompt_source: str, config: Optional[Dict[str, object]] = None) -> Optional[Dict[str, object]]:
    if str(node_type or "").strip().lower() not in {"builder", "debugger"}:
        return None
    prompt = str(prompt_source or "").strip()
    if (
        str(node_type or "").strip().lower() == "builder"
        and _looks_like_greenfield_builder_task(prompt)
    ):
        return None
    workspace = ""
    if isinstance(config, dict):
        workspace = str(config.get("workspace", "") or "").strip()
    if not is_repo_edit_task(prompt, workspace):
        return None
    repo_root = resolve_repo_root(workspace, prompt)
    if repo_root is None:
        return None

    snapshot = _repo_snapshot(str(repo_root))
    languages = ", ".join(f"{name}×{count}" for name, count in snapshot.get("languages", [])) or "unknown"

    manifest_lines: List[str] = []
    for manifest in snapshot.get("manifests", []):
        scripts = snapshot.get("package_scripts", {}).get(manifest, [])
        if scripts:
            manifest_lines.append(f"- {manifest} (scripts: {', '.join(scripts[:6])})")
        else:
            manifest_lines.append(f"- {manifest}")

    entrypoint_lines = [f"- {item}" for item in snapshot.get("entrypoints", [])[:6]]
    test_lines = [f"- {item}" for item in snapshot.get("tests", [])[:6]]
    key_file_lines = [f"- {item}" for item in snapshot.get("key_files", [])[:8]]
    verification_lines = [f"- {item}" for item in snapshot.get("verification_commands", [])[:4]]
    tree_lines = [line if line.startswith("- ") else line for line in snapshot.get("tree_slice", [])[:14]]

    prompt_lines = [
        f"Repo root: {repo_root}",
        f"Dominant languages: {languages}",
    ]
    if manifest_lines:
        prompt_lines.append("Manifests and runtimes:")
        prompt_lines.extend(manifest_lines)
    if entrypoint_lines:
        prompt_lines.append("Likely entrypoints / high-leverage files:")
        prompt_lines.extend(entrypoint_lines)
    if test_lines:
        prompt_lines.append("Tests detected:")
        prompt_lines.extend(test_lines)
    if verification_lines:
        prompt_lines.append("Suggested verification commands:")
        prompt_lines.extend(verification_lines)
    if key_file_lines:
        prompt_lines.append("Files worth reading before editing:")
        prompt_lines.extend(key_file_lines)
    if tree_lines:
        prompt_lines.append("Repo map slice:")
        prompt_lines.extend(tree_lines)

    prompt_block = "\n".join(prompt_lines).strip()
    activity_note = (
        f"已注入仓库地图：{repo_root.name}"
        + (f"；入口 {', '.join(snapshot.get('entrypoints', [])[:3])}" if snapshot.get("entrypoints") else "")
        + (f"；建议验证 {' / '.join(snapshot.get('verification_commands', [])[:2])}" if snapshot.get("verification_commands") else "")
    )

    return {
        "repo_root": str(repo_root),
        "repo_name": repo_root.name,
        "prompt_block": prompt_block,
        "activity_note": activity_note,
        "verification_commands": list(snapshot.get("verification_commands", [])),
        "entrypoints": list(snapshot.get("entrypoints", [])),
        "languages": list(snapshot.get("languages", [])),
    }


# ─────────────────────────────────────────────────────────────────────────────
# v6.5 Aider-style repo map: tree-sitter + NetworkX personalized PageRank
# Falls back to regex-only extraction if tree_sitter_languages isn't available.
# Ref: https://aider.chat/2023/10/22/repomap.html
# ─────────────────────────────────────────────────────────────────────────────
try:
    from tree_sitter_languages import get_language, get_parser  # type: ignore
    _HAS_TREE_SITTER = True
except Exception:
    _HAS_TREE_SITTER = False

try:
    import networkx as _nx  # type: ignore
    _HAS_NETWORKX = True
except Exception:
    _HAS_NETWORKX = False

_TS_LANG_BY_SUFFIX = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".rb": "ruby", ".php": "php",
}

_DEF_QUERIES = {
    "python": "(function_definition name:(identifier)@name) (class_definition name:(identifier)@name)",
    "javascript": "(function_declaration name:(identifier)@name) (class_declaration name:(identifier)@name) (method_definition name:(property_identifier)@name)",
    "typescript": "(function_declaration name:(identifier)@name) (class_declaration name:(type_identifier)@name) (method_signature name:(property_identifier)@name) (interface_declaration name:(type_identifier)@name)",
    "tsx": "(function_declaration name:(identifier)@name) (class_declaration name:(type_identifier)@name) (method_definition name:(property_identifier)@name)",
    "go": "(function_declaration name:(identifier)@name) (method_declaration name:(field_identifier)@name) (type_spec name:(type_identifier)@name)",
    "rust": "(function_item name:(identifier)@name) (struct_item name:(type_identifier)@name) (impl_item type:(type_identifier)@name)",
    "java": "(method_declaration name:(identifier)@name) (class_declaration name:(identifier)@name)",
}

_IDENT_RE = re.compile(r"\b([A-Z_][A-Za-z0-9_]{2,}|[a-z_][A-Za-z0-9_]{3,})\b")
_DEF_REGEX = {
    ".py": re.compile(r"^\s*(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M),
    ".js": re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M),
    ".ts": re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M),
    ".go": re.compile(r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.M),
    ".rs": re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M),
}
for _alias in (".jsx",):
    _DEF_REGEX[_alias] = _DEF_REGEX[".js"]
for _alias in (".tsx",):
    _DEF_REGEX[_alias] = _DEF_REGEX[".ts"]


def _extract_defs_refs(path: Path, text: str) -> Tuple[List[str], List[str]]:
    """Return (definitions, references) for a source file."""
    suffix = path.suffix.lower()
    defs: List[str] = []
    if _HAS_TREE_SITTER and suffix in _TS_LANG_BY_SUFFIX:
        lang_name = _TS_LANG_BY_SUFFIX[suffix]
        try:
            parser = get_parser(lang_name)
            tree = parser.parse(text.encode("utf8", errors="ignore"))
            query_src = _DEF_QUERIES.get(lang_name, "")
            if query_src:
                query = get_language(lang_name).query(query_src)
                for node, _cap in query.captures(tree.root_node):
                    try:
                        defs.append(node.text.decode("utf8", errors="ignore"))
                    except Exception:
                        pass
        except Exception:
            defs = []
    if not defs:
        rx = _DEF_REGEX.get(suffix)
        if rx is not None:
            defs = rx.findall(text)
    # references: every identifier-like token in the file, minus its own defs.
    all_idents = _IDENT_RE.findall(text)
    def_set = set(defs)
    refs = [ident for ident in all_idents if ident not in def_set]
    return defs, refs


def rank_repo_symbols(
    repo_root: Path | str,
    chat_files: Optional[Iterable[str]] = None,
    mentioned_idents: Optional[Iterable[str]] = None,
    token_budget: int = 1024,
    max_files: int = 400,
) -> List[Dict[str, object]]:
    """
    Aider-style personalized PageRank over a DiGraph of files→referenced-defs.

    Returns a list of {"file": relpath, "name": ident, "score": float, "line": 0}
    entries, truncated to roughly `token_budget` tokens (≈4 chars each).
    """
    root = Path(repo_root)
    if not root.is_dir():
        return []
    chat_set = {str(p) for p in (chat_files or [])}
    mention_set = {str(i) for i in (mentioned_idents or [])}

    files_seen = 0
    file_defs: Dict[str, List[str]] = {}
    file_refs: Dict[str, List[str]] = {}
    def_to_files: Dict[str, List[str]] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for fname in filenames:
            suffix = Path(fname).suffix.lower()
            if suffix not in LANGUAGE_BY_SUFFIX:
                continue
            full = Path(dirpath) / fname
            try:
                if full.stat().st_size > 512 * 1024:
                    continue
                text = full.read_text(encoding="utf8", errors="ignore")
            except Exception:
                continue
            rel = _safe_rel(full, root)
            defs, refs = _extract_defs_refs(full, text)
            if not defs and not refs:
                continue
            file_defs[rel] = defs
            file_refs[rel] = refs
            for d in defs:
                def_to_files.setdefault(d, []).append(rel)
            files_seen += 1
            if files_seen >= max_files:
                break
        if files_seen >= max_files:
            break

    if not file_defs:
        return []

    if not _HAS_NETWORKX:
        # Fallback: naive score = (# incoming refs) * mention boost.
        scored: List[Tuple[str, str, float]] = []
        for ident, defining_files in def_to_files.items():
            incoming = sum(
                refs.count(ident) for refs in file_refs.values()
            )
            boost = 10.0 if ident in mention_set else 1.0
            for df in defining_files:
                chat_boost = 50.0 if df in chat_set else 1.0
                scored.append((df, ident, float(incoming) * boost * chat_boost))
        scored.sort(key=lambda t: t[2], reverse=True)
        out: List[Dict[str, object]] = []
        used = 0
        for df, ident, sc in scored:
            entry = {"file": df, "name": ident, "score": round(sc, 3), "line": 0}
            cost = len(df) + len(ident) + 16
            if used + cost > token_budget * 4:
                break
            out.append(entry)
            used += cost
        return out

    graph = _nx.DiGraph()
    for rel in file_defs:
        graph.add_node(rel)
    for src_file, refs in file_refs.items():
        ref_counts: Dict[str, int] = {}
        for r in refs:
            ref_counts[r] = ref_counts.get(r, 0) + 1
        for ident, count in ref_counts.items():
            for tgt_file in def_to_files.get(ident, []):
                if tgt_file == src_file:
                    continue
                w = float(count) * (10.0 if ident in mention_set else 1.0)
                if graph.has_edge(src_file, tgt_file):
                    graph[src_file][tgt_file]["weight"] += w
                else:
                    graph.add_edge(src_file, tgt_file, weight=w, idents=ident)

    personalization: Dict[str, float] = {}
    for node in graph.nodes:
        personalization[node] = 50.0 if node in chat_set else 1.0
    try:
        ranks = _nx.pagerank(graph, alpha=0.85, personalization=personalization, weight="weight")
    except Exception:
        ranks = {n: 1.0 for n in graph.nodes}

    # Distribute file rank across that file's defs, boosting mentioned idents.
    results: List[Dict[str, object]] = []
    for rel, defs in file_defs.items():
        base = float(ranks.get(rel, 0.0))
        if not defs or base <= 0:
            continue
        share = base / max(len(defs), 1)
        for d in defs:
            boost = 10.0 if d in mention_set else 1.0
            results.append({"file": rel, "name": d, "score": round(share * boost, 6), "line": 0})

    results.sort(key=lambda e: float(e["score"]), reverse=True)
    out: List[Dict[str, object]] = []
    used = 0
    for entry in results:
        cost = len(str(entry["file"])) + len(str(entry["name"])) + 16
        if used + cost > token_budget * 4:
            break
        out.append(entry)
        used += cost
    return out


def render_ranked_map(
    repo_root: Path | str,
    chat_files: Optional[Iterable[str]] = None,
    mentioned_idents: Optional[Iterable[str]] = None,
    token_budget: int = 1024,
) -> str:
    """Human-readable block for injection into the chat system prompt."""
    entries = rank_repo_symbols(repo_root, chat_files, mentioned_idents, token_budget)
    if not entries:
        return ""
    lines = ["## Repo Map (top symbols, PageRank)"]
    by_file: Dict[str, List[str]] = {}
    for e in entries:
        by_file.setdefault(str(e["file"]), []).append(str(e["name"]))
    for fpath, names in list(by_file.items())[:60]:
        lines.append(f"- `{fpath}`: {', '.join(names[:6])}")
    return "\n".join(lines)
