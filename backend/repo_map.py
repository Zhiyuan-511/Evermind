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
    has_file_hint = bool(FILE_HINT_RE.search(prompt))
    has_failure_hint = bool(re.search(r"(报错|错误|bug|error|traceback|stack trace|failing test|build failed|lint failed)", prompt, re.IGNORECASE))
    return bool(
        (has_edit_intent and (has_repo_hint or has_file_hint or has_failure_hint))
        or (has_repo_hint and has_failure_hint)
    )


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
    workspace = ""
    if isinstance(config, dict):
        workspace = str(config.get("workspace", "") or "").strip()
    if not is_repo_edit_task(prompt_source, workspace):
        return None
    repo_root = resolve_repo_root(workspace, prompt_source)
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
