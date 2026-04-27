"""
Evermind GitHub integration (v6.4.4 — maintainer 2026-04-21).

Minimal but functional: PAT-based auth (one-time input), publish current
project to GitHub with a single click, subsequent commit & push loop.

Endpoints:
  GET  /api/git/status          → {is_repo, branch, dirty_files, has_remote, remote_url, authenticated, username}
  POST /api/git/auth/pat        → body:{token} validate + store in keyring
  DELETE /api/git/auth          → remove stored token
  POST /api/git/publish         → body:{name, private, commit_msg} one-click init+commit+create+push
  POST /api/git/commit_push     → body:{message} second-time+ commits
  POST /api/git/open            → returns url to open in browser

Security: PAT stored in macOS Keychain via keyring (fallback: 0600
~/.evermind/git_token). Never returned to frontend after save.

Design reference: agent's 750-line plan from earlier research.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException

router = APIRouter(prefix="/api/git", tags=["git"])

# ── Constants ──
SERVICE_NAME = "evermind-github"
KEY_NAME = "pat"
FALLBACK_FILE = Path.home() / ".evermind" / "git_token"
GITHUB_API = "https://api.github.com"
DEFAULT_PROJECT_ROOT = Path(os.getenv("EVERMIND_OUTPUT_DIR") or "/tmp/evermind_output")
DEFAULT_GITIGNORE = """# Evermind defaults
.DS_Store
*.log
*.pyc
__pycache__/
node_modules/
.env
.env.local
_evermind_runtime/
_previous_run_*/
_stable_previews/
_builder_backups/
"""


# ── Token storage (keyring preferred, file fallback) ──
def _get_token() -> str:
    try:
        import keyring
        tok = keyring.get_password(SERVICE_NAME, KEY_NAME)
        if tok:
            return str(tok)
    except Exception:
        pass
    try:
        if FALLBACK_FILE.exists():
            return FALLBACK_FILE.read_text().strip()
    except Exception:
        pass
    return ""


def _set_token(token: str) -> None:
    token = (token or "").strip()
    ok = False
    try:
        import keyring
        keyring.set_password(SERVICE_NAME, KEY_NAME, token)
        ok = True
    except Exception:
        pass
    if not ok:
        try:
            FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
            FALLBACK_FILE.write_text(token)
            os.chmod(FALLBACK_FILE, 0o600)
        except Exception as exc:
            raise HTTPException(500, f"could not persist token: {exc}")


def _clear_token() -> None:
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, KEY_NAME)
    except Exception:
        pass
    try:
        if FALLBACK_FILE.exists():
            FALLBACK_FILE.unlink()
    except Exception:
        pass


# ── GitHub REST helpers ──
def _github_request(path: str, method: str = "GET", body: Optional[Dict] = None, token: str = "") -> Dict[str, Any]:
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "evermind/6.4",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            return {"ok": True, "status": resp.status, "body": json.loads(text) if text else {}}
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode()
            body_json = json.loads(body_text) if body_text.startswith("{") else {"raw": body_text[:400]}
        except Exception:
            body_json = {}
        return {"ok": False, "status": e.code, "body": body_json}
    except Exception as e:
        return {"ok": False, "status": 0, "body": {"error": str(e)[:300]}}


# ── Git shell helpers ──
def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


def _resolve_project_root(path: Optional[str] = None) -> Path:
    # v7.4 SECURITY: path must be DEFAULT_PROJECT_ROOT or strictly inside it.
    # Without this whitelist, any caller (XSS in a future preview iframe,
    # CSRF, malicious extension) could request `path=/Users/<victim>/private`
    # and have backend run `git init + git add -A + git push` against an
    # arbitrary local directory — full data exfiltration vector.
    if not path:
        return DEFAULT_PROJECT_ROOT
    p = Path(path).expanduser().resolve()
    root_resolved = DEFAULT_PROJECT_ROOT.resolve()
    try:
        p.relative_to(root_resolved)
    except ValueError:
        raise HTTPException(400, "project path must be inside the evermind output directory")
    if not p.exists():
        raise HTTPException(400, f"project path does not exist: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"project path is not a directory: {p}")
    return p


def _ensure_gitignore(root: Path) -> None:
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(DEFAULT_GITIGNORE)


# ── Endpoints ──
@router.get("/status")
def git_status(path: Optional[str] = None) -> Dict[str, Any]:
    """Show repo + auth state for the UI."""
    tok = _get_token()
    result: Dict[str, Any] = {
        "authenticated": bool(tok),
        "username": None,
        "is_repo": False,
        "branch": None,
        "dirty_files": 0,
        "has_remote": False,
        "remote_url": None,
        "project_path": str(DEFAULT_PROJECT_ROOT),
        "project_exists": DEFAULT_PROJECT_ROOT.exists(),
    }
    if tok:
        r = _github_request("/user", token=tok)
        if r["ok"]:
            result["username"] = r["body"].get("login")
        else:
            result["authenticated"] = False
            result["auth_error"] = f"GitHub /user returned {r['status']}"
    try:
        root = _resolve_project_root(path)
    except HTTPException:
        return result
    result["project_path"] = str(root)
    if (root / ".git").is_dir():
        result["is_repo"] = True
        r = _git("branch", "--show-current", cwd=root)
        result["branch"] = (r.stdout or "").strip() or "main"
        r = _git("status", "--porcelain", cwd=root)
        result["dirty_files"] = len([l for l in r.stdout.splitlines() if l.strip()])
        r = _git("remote", "get-url", "origin", cwd=root)
        if r.returncode == 0 and r.stdout.strip():
            result["has_remote"] = True
            result["remote_url"] = r.stdout.strip()
    return result


@router.get("/diff")
def git_diff(path: Optional[str] = None, staged: bool = False, context: int = 3) -> Dict[str, Any]:
    """v7.0 (maintainer 2026-04-24): diff endpoint for the VSCode-style source
    control panel. Returns per-file unified diff hunks so the UI can render
    green/red coloring. If repo is not a git repo or empty, returns
    empty list without raising (UI handles gracefully).
    """
    try:
        root = _resolve_project_root(path)
    except HTTPException:
        return {"is_repo": False, "files": []}
    if not (root / ".git").is_dir():
        # Auto-init so new projects surface changes
        try:
            _git("init", cwd=root)
            _ensure_gitignore(root)
            _git("add", "-A", cwd=root)
            _git("-c", "user.name=Evermind", "-c", "user.email=evermind@local",
                 "commit", "-m", "initial snapshot", cwd=root)
        except Exception:
            pass
    if not (root / ".git").is_dir():
        return {"is_repo": False, "files": []}

    # Per-file list via name-status (modified/added/deleted)
    args = ["diff", "--name-status"]
    if staged:
        args.append("--staged")
    r = _git(*args, cwd=root)
    files = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        status_code = parts[0][0]  # M / A / D / R / ?
        fpath = parts[1]
        status_map = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "?": "untracked"}
        files.append({"path": fpath, "status": status_map.get(status_code, status_code)})

    # Untracked files (not in diff output)
    r_untracked = _git("ls-files", "--others", "--exclude-standard", cwd=root)
    for line in (r_untracked.stdout or "").splitlines():
        line = line.strip()
        if line:
            files.append({"path": line, "status": "untracked"})

    # Full unified diff for colouring
    diff_args = ["diff", f"--unified={max(0, min(int(context), 20))}"]
    if staged:
        diff_args.append("--staged")
    r_diff = _git(*diff_args, cwd=root)
    unified = r_diff.stdout or ""

    # Parse unified diff → per-file hunks (list of {line_type, content})
    per_file: Dict[str, List[Dict[str, Any]]] = {}
    _cur_file = ""
    for raw in unified.splitlines():
        if raw.startswith("diff --git "):
            # "diff --git a/foo b/foo"
            bits = raw.split(" b/", 1)
            _cur_file = bits[1] if len(bits) == 2 else ""
            per_file.setdefault(_cur_file, [])
            continue
        if raw.startswith(("index ", "+++ ", "--- ", "new file", "deleted file", "similarity", "rename ")):
            continue
        if not _cur_file:
            continue
        if raw.startswith("@@"):
            per_file[_cur_file].append({"type": "hunk_header", "content": raw})
        elif raw.startswith("+") and not raw.startswith("+++"):
            per_file[_cur_file].append({"type": "add", "content": raw[1:]})
        elif raw.startswith("-") and not raw.startswith("---"):
            per_file[_cur_file].append({"type": "del", "content": raw[1:]})
        else:
            per_file[_cur_file].append({"type": "ctx", "content": raw[1:] if raw.startswith(" ") else raw})

    for f in files:
        f["hunks"] = per_file.get(f["path"], [])

    # Add stats
    summary = {
        "total_files": len(files),
        "added_lines": sum(1 for hs in per_file.values() for h in hs if h.get("type") == "add"),
        "deleted_lines": sum(1 for hs in per_file.values() for h in hs if h.get("type") == "del"),
    }
    return {"is_repo": True, "project_path": str(root), "staged": staged, "files": files, "summary": summary}


@router.post("/auth/pat")
def save_pat(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Validate + store a GitHub Personal Access Token."""
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token is empty")
    r = _github_request("/user", token=token)
    if not r["ok"]:
        raise HTTPException(
            401,
            f"GitHub rejected the token ({r['status']}): {r['body'].get('message', 'unknown error')}",
        )
    username = r["body"].get("login")
    _set_token(token)
    return {"ok": True, "username": username}


@router.delete("/auth")
def delete_pat() -> Dict[str, Any]:
    _clear_token()
    return {"ok": True}


@router.post("/publish")
def publish(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """One-click: init (if needed) → commit → create repo on GitHub → push."""
    tok = _get_token()
    if not tok:
        raise HTTPException(401, "not authenticated — save a GitHub PAT first")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "repo name required")
    private = bool(body.get("private", True))
    commit_msg = str(body.get("commit_msg") or "Initial commit from Evermind").strip() or "Initial commit"
    project_path = body.get("path")
    root = _resolve_project_root(project_path)

    steps = []

    # 1. git init if needed
    if not (root / ".git").is_dir():
        r = _git("init", "-b", "main", cwd=root)
        if r.returncode != 0:
            # fall back to default branch then rename
            _git("init", cwd=root)
            _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=root)
        steps.append("git init")

    _ensure_gitignore(root)

    # 2. configure user if missing
    user_email = _git("config", "user.email", cwd=root).stdout.strip()
    if not user_email:
        _git("config", "user.email", "evermind@local.dev", cwd=root)
        _git("config", "user.name", "Evermind User", cwd=root)

    # 3. stage + commit
    _git("add", "-A", cwd=root)
    r = _git("commit", "-m", commit_msg, cwd=root)
    # commit may fail if nothing to commit — that's OK if there's already a HEAD
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr).lower():
        head = _git("rev-parse", "HEAD", cwd=root)
        if head.returncode != 0:
            raise HTTPException(500, f"git commit failed: {r.stderr[:400]}")
    steps.append("git commit")

    # 4. check github for existing repo
    user_info = _github_request("/user", token=tok)
    if not user_info["ok"]:
        raise HTTPException(401, "token appears invalid now")
    username = user_info["body"].get("login")
    exists = _github_request(f"/repos/{username}/{name}", token=tok)

    # 5. create repo if it doesn't exist
    remote_url = None
    if not exists["ok"] and exists["status"] == 404:
        create = _github_request(
            "/user/repos",
            method="POST",
            body={"name": name, "private": private, "auto_init": False},
            token=tok,
        )
        if not create["ok"]:
            msg = create["body"].get("message", f"status {create['status']}")
            raise HTTPException(400, f"GitHub refused to create repo: {msg}")
        remote_url = create["body"].get("clone_url") or f"https://github.com/{username}/{name}.git"
        steps.append(f"created github.com/{username}/{name}")
    elif exists["ok"]:
        remote_url = exists["body"].get("clone_url") or f"https://github.com/{username}/{name}.git"
        steps.append(f"using existing github.com/{username}/{name}")
    else:
        raise HTTPException(400, f"GitHub returned {exists['status']} checking the repo")

    # 6. set remote
    current_remote = _git("remote", "get-url", "origin", cwd=root).stdout.strip()
    if current_remote != remote_url:
        if current_remote:
            _git("remote", "set-url", "origin", remote_url, cwd=root)
        else:
            _git("remote", "add", "origin", remote_url, cwd=root)

    # 7. push. Use https with embedded token for auth (keeps user's git config clean).
    # v7.4 SECURITY: scrub the token from any error path that flows back to the
    # frontend. git's stderr commonly echoes the full URL on auth failures
    # ("fatal: unable to access 'https://x-access-token:ghp_xxx@github.com/...'"),
    # which would leak the user's PAT into HTTP error bodies + browser logs.
    authed_url = remote_url.replace("https://", f"https://x-access-token:{tok}@")
    push = _git("push", "-u", authed_url, "main", cwd=root)
    if push.returncode != 0:
        err = (push.stderr or push.stdout or "")[:400]
        # Mask any embedded credential before surfacing
        err = re.sub(r"https://[^@\s]+@", "https://***@", err)
        err = err.replace(tok, "***") if tok else err
        raise HTTPException(400, f"git push failed: {err}")
    steps.append("git push")

    html_url = remote_url.replace(".git", "").replace("https://x-access-token:", "").replace(f"{tok}@", "")

    return {
        "ok": True,
        "steps": steps,
        "remote_url": remote_url,
        "html_url": html_url,
        "repo_name": f"{username}/{name}",
    }


@router.post("/commit_push")
def commit_push(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Second-time+ commits. Requires repo already published."""
    tok = _get_token()
    if not tok:
        raise HTTPException(401, "not authenticated")
    root = _resolve_project_root(body.get("path"))
    if not (root / ".git").is_dir():
        raise HTTPException(400, "not a git repo — use /publish first")
    remote = _git("remote", "get-url", "origin", cwd=root).stdout.strip()
    if not remote:
        raise HTTPException(400, "no origin remote — use /publish first")
    msg = str(body.get("message") or "").strip() or f"Update from Evermind ({time.strftime('%Y-%m-%d %H:%M')})"
    _git("add", "-A", cwd=root)
    r = _git("commit", "-m", msg, cwd=root)
    commit_happened = r.returncode == 0 and "nothing to commit" not in (r.stdout + r.stderr).lower()
    authed = remote.replace("https://", f"https://x-access-token:{tok}@")
    push = _git("push", authed, "main", cwd=root)
    if push.returncode != 0:
        raise HTTPException(400, f"git push failed: {(push.stderr or push.stdout)[:400]}")
    return {"ok": True, "committed": commit_happened, "pushed": True, "message": msg}


@router.get("/history")
def history(path: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
    """Recent commits for the UI history panel."""
    root = _resolve_project_root(path)
    if not (root / ".git").is_dir():
        return {"commits": []}
    r = _git("log", f"-{max(1, min(limit, 50))}", "--pretty=format:%h|%an|%ar|%s", cwd=root)
    commits = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({"sha": parts[0], "author": parts[1], "ago": parts[2], "subject": parts[3]})
    return {"commits": commits}
