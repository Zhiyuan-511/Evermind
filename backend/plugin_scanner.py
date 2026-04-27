"""
plugin_scanner.py — Probe what plugins/skills/MCP servers a user has installed
in each CLI agent (Claude / Gemini / Codex / Kimi / Qwen), so the orchestrator
can inject "you have X available" hints into prompts.

Strategy per CLI:
  - Prefer `--json` subcommands when they exist (codex mcp, claude plugin)
  - Fallback to filesystem scan of well-known directories for everything else
  - NEVER trigger OAuth — all calls are read-only config inspection
  - Hard timeout (default 8s) on every subprocess to avoid hangs

Returns a uniform dict per CLI:
  {
    "skills":               [{"name", "description", "path", "enabled"}],
    "mcp_servers":          [{"name", "command", "args", "enabled", "transport"}],
    "extensions_or_plugins":[{"name", "version", "scope", "enabled"}],
    "agents":               [{"name", "model", "scope"}],
    "hooks":                [{"event", "matcher", "command"}],
    "scan_method":          "cli_subcommand" | "filesystem" | "mixed",
    "errors":               [str, ...],
  }

Last verified: 2026-04-26 against:
  claude  v? (Claude Code, ~/.local/bin/claude)
  gemini  v?
  codex   v?
  kimi    v1.39
  qwen    v0.15
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

HOME = Path.home()
DEFAULT_TIMEOUT_S = 8


# --------------------------------------------------------------------- helpers


def _run(cmd: list[str], timeout: int = DEFAULT_TIMEOUT_S) -> tuple[int, str, str]:
    """Run a command with stdin closed; return (rc, stdout, stderr).
    Never raises — captures TimeoutExpired and FileNotFoundError as rc=-1."""
    try:
        p = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return -1, "", f"binary not found: {e}"
    except Exception as e:  # pragma: no cover
        return -1, "", f"{type(e).__name__}: {e}"


def _read_skill_md(skill_dir: Path) -> dict[str, str]:
    """Parse SKILL.md frontmatter (YAML between --- markers) for name/description."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return {"name": skill_dir.name, "description": ""}
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"name": skill_dir.name, "description": ""}
    name = skill_dir.name
    desc = ""
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        if m := re.search(r"^name:\s*(.+?)\s*$", fm, re.MULTILINE):
            name = m.group(1).strip().strip('"').strip("'")
        if m := re.search(r"^description:\s*(.+?)\s*$", fm, re.MULTILINE | re.DOTALL):
            desc = m.group(1).strip().strip('"').strip("'").splitlines()[0]
    return {"name": name, "description": desc[:300]}


def _scan_skills_dir(root: Path) -> list[dict[str, Any]]:
    """Enumerate <root>/<name>/SKILL.md entries."""
    out: list[dict[str, Any]] = []
    if not root.exists() or not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "SKILL.md").exists():
            meta = _read_skill_md(child)
            out.append({
                "name": meta["name"],
                "description": meta["description"],
                "path": str(child),
                "enabled": True,
            })
    return out


def _empty_result() -> dict[str, Any]:
    return {
        "skills": [],
        "mcp_servers": [],
        "extensions_or_plugins": [],
        "agents": [],
        "hooks": [],
        "scan_method": "filesystem",
        "errors": [],
    }


# --------------------------------------------------------------------- claude


def scan_claude_capabilities(
    claude_bin: str = str(HOME / ".local/bin/claude"),
) -> dict[str, Any]:
    res = _empty_result()
    res["scan_method"] = "mixed"

    # Plugins via JSON CLI (cheap, no API)
    if shutil.which(claude_bin):
        rc, out, err = _run([claude_bin, "plugin", "list", "--json"])
        if rc == 0 and out.strip():
            try:
                for p in json.loads(out):
                    res["extensions_or_plugins"].append({
                        "name": p.get("id", ""),
                        "version": p.get("version", "unknown"),
                        "scope": p.get("scope", "user"),
                        "enabled": bool(p.get("enabled", True)),
                    })
            except json.JSONDecodeError as e:
                res["errors"].append(f"plugin list json: {e}")
        elif rc != 0:
            res["errors"].append(f"plugin list rc={rc} {err.strip()[:120]}")

    # MCP servers — DO NOT call `claude mcp list` (spawns servers).
    # Read both ~/.claude/.mcp.json AND settings.json enabledMcpjsonServers.
    seen_mcp: set[str] = set()
    mcp_json = HOME / ".claude/.mcp.json"
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text())
            for name, spec in (data.get("mcpServers") or {}).items():
                seen_mcp.add(name)
                res["mcp_servers"].append({
                    "name": name,
                    "command": spec.get("command", ""),
                    "args": spec.get("args", []) or [],
                    "enabled": True,
                    "transport": spec.get("type", "stdio"),
                })
        except (OSError, json.JSONDecodeError) as e:
            res["errors"].append(f".mcp.json: {e}")

    settings = HOME / ".claude/settings.json"
    if settings.exists():
        try:
            sdata = json.loads(settings.read_text())
            for name, spec in (sdata.get("mcpServers") or {}).items():
                if name in seen_mcp:
                    continue
                seen_mcp.add(name)
                res["mcp_servers"].append({
                    "name": name,
                    "command": spec.get("command", ""),
                    "args": spec.get("args", []) or [],
                    "enabled": True,
                    "transport": spec.get("type", "stdio"),
                })
            # Hooks
            hooks = sdata.get("hooks") or {}
            for event, items in hooks.items():
                for item in items or []:
                    matcher = item.get("matcher", "")
                    for h in item.get("hooks", []) or []:
                        res["hooks"].append({
                            "event": event,
                            "matcher": matcher,
                            "command": h.get("command", ""),
                        })
        except (OSError, json.JSONDecodeError) as e:
            res["errors"].append(f"settings.json: {e}")

    # Skills — filesystem only (no CLI subcommand exists)
    res["skills"] = _scan_skills_dir(HOME / ".claude/skills")

    # Agents — try CLI first (text format), fallback to filesystem
    if shutil.which(claude_bin):
        rc, out, _ = _run([claude_bin, "agents"])
        if rc == 0 and out.strip():
            for line in out.splitlines():
                m = re.match(r"^\s+([a-zA-Z0-9_\-:]+)\s*·\s*(\S+)", line)
                if m:
                    res["agents"].append({
                        "name": m.group(1),
                        "model": m.group(2),
                        "scope": "cli",
                    })
    if not res["agents"]:
        agents_dir = HOME / ".claude/agents"
        if agents_dir.exists():
            for f in sorted(agents_dir.glob("*.md")):
                res["agents"].append({
                    "name": f.stem,
                    "model": "unknown",
                    "scope": "user",
                })

    return res


# --------------------------------------------------------------------- gemini


def scan_gemini_capabilities(
    gemini_bin: str = "/opt/homebrew/bin/gemini",
) -> dict[str, Any]:
    res = _empty_result()
    res["scan_method"] = "mixed"

    # Skills — Gemini DOES have `skills list` (text). Parse blank-line blocks.
    if shutil.which(gemini_bin):
        rc, out, err = _run([gemini_bin, "skills", "list"])
        if rc == 0 and out.strip():
            block: dict[str, str] = {}
            for line in out.splitlines():
                line_stripped = line.strip()
                if not line_stripped:
                    if block.get("name"):
                        res["skills"].append({
                            "name": block["name"],
                            "description": block.get("description", ""),
                            "path": block.get("path", ""),
                            "enabled": block.get("enabled", "true") == "true",
                        })
                    block = {}
                    continue
                hdr = re.match(r"^([\w\-]+)\s*\[(\w+)\]\s*$", line_stripped)
                if hdr:
                    block = {"name": hdr.group(1), "enabled": "true" if hdr.group(2).lower() == "enabled" else "false"}
                elif line_stripped.startswith("Description:"):
                    block["description"] = line_stripped[len("Description:"):].strip()
                elif line_stripped.startswith("Location:"):
                    block["path"] = line_stripped[len("Location:"):].strip()
            if block.get("name"):
                res["skills"].append({
                    "name": block["name"],
                    "description": block.get("description", ""),
                    "path": block.get("path", ""),
                    "enabled": block.get("enabled", "true") == "true",
                })
        elif rc != 0:
            res["errors"].append(f"skills list rc={rc} {err.strip()[:120]}")

    # MCP — read settings.json directly (faster than CLI; no health check)
    settings = HOME / ".gemini/settings.json"
    if settings.exists():
        try:
            sdata = json.loads(settings.read_text())
            for name, spec in (sdata.get("mcpServers") or {}).items():
                res["mcp_servers"].append({
                    "name": name,
                    "command": spec.get("command", ""),
                    "args": spec.get("args", []) or [],
                    "enabled": True,
                    "transport": "http" if spec.get("httpUrl") else "stdio",
                })
        except (OSError, json.JSONDecodeError) as e:
            res["errors"].append(f"gemini settings.json: {e}")

    # Extensions — filesystem (CLI is text-only and slow)
    ext_dir = HOME / ".gemini/extensions"
    if ext_dir.exists():
        for child in sorted(ext_dir.iterdir()):
            if child.is_dir():
                manifest = child / "gemini-extension.json"
                version = "unknown"
                if manifest.exists():
                    try:
                        version = json.loads(manifest.read_text()).get("version", "unknown")
                    except (OSError, json.JSONDecodeError):
                        pass
                res["extensions_or_plugins"].append({
                    "name": child.name,
                    "version": version,
                    "scope": "user",
                    "enabled": True,
                })

    # Hooks — Gemini only has `hooks migrate`, no listing. settings.json may have a hooks key.
    if settings.exists():
        try:
            sdata = json.loads(settings.read_text())
            for event, items in (sdata.get("hooks") or {}).items():
                for item in items or []:
                    res["hooks"].append({
                        "event": event,
                        "matcher": item.get("matcher", ""),
                        "command": str(item.get("hooks", item.get("command", ""))),
                    })
        except (OSError, json.JSONDecodeError):
            pass

    return res


# --------------------------------------------------------------------- codex


def scan_codex_capabilities(
    codex_bin: str = "/opt/homebrew/bin/codex",
) -> dict[str, Any]:
    res = _empty_result()
    res["scan_method"] = "mixed"

    # MCP — `codex mcp list --json` is the cleanest interface in this whole study
    if shutil.which(codex_bin):
        rc, out, err = _run([codex_bin, "mcp", "list", "--json"])
        if rc == 0 and out.strip():
            try:
                for s in json.loads(out):
                    transport = s.get("transport") or {}
                    res["mcp_servers"].append({
                        "name": s.get("name", ""),
                        "command": transport.get("command", ""),
                        "args": transport.get("args", []) or [],
                        "enabled": bool(s.get("enabled", True)),
                        "transport": transport.get("type", "stdio"),
                    })
            except json.JSONDecodeError as e:
                res["errors"].append(f"codex mcp json: {e}")
        elif rc != 0:
            res["errors"].append(f"codex mcp rc={rc} {err.strip()[:120]}")

    # Skills — no CLI; scan ~/.codex/skills/ (and ~/.agents/skills/ as Codex also reads it)
    res["skills"] = _scan_skills_dir(HOME / ".codex/skills")

    # Plugins — `codex plugin marketplace` is the only listing interface, and it's marketplace-scoped.
    # Codex 0.x doesn't track installed plugins as first-class objects, so leave empty.

    # Hooks — Codex has no hook system. Skip.

    return res


# --------------------------------------------------------------------- kimi


def scan_kimi_capabilities(
    kimi_bin: str = str(HOME / ".local/bin/kimi"),
) -> dict[str, Any]:
    res = _empty_result()
    res["scan_method"] = "mixed"

    # MCP — read ~/.kimi/mcp.json directly (faster than CLI; CLI emits stderr warning)
    mcp_json = HOME / ".kimi/mcp.json"
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text())
            for name, spec in (data.get("mcpServers") or {}).items():
                res["mcp_servers"].append({
                    "name": name,
                    "command": spec.get("command", ""),
                    "args": spec.get("args", []) or [],
                    "enabled": True,
                    "transport": spec.get("type", "stdio"),
                })
        except (OSError, json.JSONDecodeError) as e:
            res["errors"].append(f"kimi mcp.json: {e}")

    # Plugins — scan ~/.kimi/plugins/<name>/plugin.json
    plugins_dir = HOME / ".kimi/plugins"
    if plugins_dir.exists():
        for child in sorted(plugins_dir.iterdir()):
            if child.is_dir():
                manifest = child / "plugin.json"
                version = "unknown"
                if manifest.exists():
                    try:
                        version = json.loads(manifest.read_text()).get("version", "unknown")
                    except (OSError, json.JSONDecodeError):
                        pass
                res["extensions_or_plugins"].append({
                    "name": child.name,
                    "version": version,
                    "scope": "user",
                    "enabled": True,
                })

    # Skills — Kimi auto-discovers from a layered list. We replicate the brand+generic ladder.
    # Brand group: first existing wins between ~/.kimi, ~/.claude, ~/.codex.
    seen: set[str] = set()
    for brand in [HOME / ".kimi/skills", HOME / ".claude/skills", HOME / ".codex/skills"]:
        if brand.exists():
            for s in _scan_skills_dir(brand):
                if s["name"] not in seen:
                    res["skills"].append(s)
                    seen.add(s["name"])
            break  # mutually exclusive
    for generic in [HOME / ".config/agents/skills", HOME / ".agents/skills"]:
        if generic.exists():
            for s in _scan_skills_dir(generic):
                if s["name"] not in seen:
                    res["skills"].append(s)
                    seen.add(s["name"])
            break

    # extra_skill_dirs from config.toml
    cfg = HOME / ".kimi/config.toml"
    if cfg.exists():
        try:
            data = tomllib.loads(cfg.read_text())
            for d in data.get("extra_skill_dirs", []) or []:
                p = Path(os.path.expanduser(d))
                for s in _scan_skills_dir(p):
                    if s["name"] not in seen:
                        res["skills"].append(s)
                        seen.add(s["name"])
            # Hooks
            for h in data.get("hooks", []) or []:
                if isinstance(h, dict):
                    res["hooks"].append({
                        "event": h.get("event", ""),
                        "matcher": h.get("matcher", ""),
                        "command": h.get("command", ""),
                    })
        except (OSError, tomllib.TOMLDecodeError) as e:
            res["errors"].append(f"kimi config.toml: {e}")

    return res


# --------------------------------------------------------------------- qwen


def scan_qwen_capabilities(
    qwen_bin: str = "/opt/homebrew/bin/qwen",
) -> dict[str, Any]:
    res = _empty_result()
    res["scan_method"] = "mixed"

    # MCP — qwen mcp list is text; settings.json is canonical. Prefer filesystem.
    settings = HOME / ".qwen/settings.json"
    if settings.exists():
        try:
            sdata = json.loads(settings.read_text())
            for name, spec in (sdata.get("mcpServers") or {}).items():
                res["mcp_servers"].append({
                    "name": name,
                    "command": spec.get("command", ""),
                    "args": spec.get("args", []) or [],
                    "enabled": True,
                    "transport": "http" if spec.get("httpUrl") else "stdio",
                })
            for event, items in (sdata.get("hooks") or {}).items():
                for item in items or []:
                    res["hooks"].append({
                        "event": event,
                        "matcher": item.get("matcher", ""),
                        "command": str(item.get("hooks", item.get("command", ""))),
                    })
        except (OSError, json.JSONDecodeError) as e:
            res["errors"].append(f"qwen settings.json: {e}")

    # Extensions — filesystem
    ext_dir = HOME / ".qwen/extensions"
    if ext_dir.exists():
        for child in sorted(ext_dir.iterdir()):
            if child.is_dir():
                manifest = child / "qwen-extension.json"
                if not manifest.exists():
                    manifest = child / "gemini-extension.json"  # qwen is a gemini fork
                version = "unknown"
                if manifest.exists():
                    try:
                        version = json.loads(manifest.read_text()).get("version", "unknown")
                    except (OSError, json.JSONDecodeError):
                        pass
                res["extensions_or_plugins"].append({
                    "name": child.name,
                    "version": version,
                    "scope": "user",
                    "enabled": True,
                })

    # Skills — Qwen has no `skills` subcommand. Try filesystem only.
    res["skills"] = _scan_skills_dir(HOME / ".qwen/skills")

    return res


# --------------------------------------------------------------------- public


def scan_all(timeout: int = DEFAULT_TIMEOUT_S) -> dict[str, dict[str, Any]]:
    """Scan all 5 CLIs. Each scanner is independent; one failure won't block others."""
    results = {}
    for cli, fn in [
        ("claude", scan_claude_capabilities),
        ("gemini", scan_gemini_capabilities),
        ("codex", scan_codex_capabilities),
        ("kimi", scan_kimi_capabilities),
        ("qwen", scan_qwen_capabilities),
    ]:
        try:
            results[cli] = fn()
        except Exception as e:  # pragma: no cover
            results[cli] = {**_empty_result(), "errors": [f"scanner crashed: {e}"]}
    return results


def render_prompt_hint(cli: str, caps: dict[str, Any], max_skills: int = 8) -> str:
    """Render a compact 'available capabilities' block to inject into LLM prompt."""
    lines = [f"## Available in {cli}:"]
    if caps.get("skills"):
        lines.append(f"Skills ({len(caps['skills'])}):")
        for s in caps["skills"][:max_skills]:
            desc = (s.get("description") or "")[:100]
            lines.append(f"  - {s['name']}: {desc}")
    if caps.get("mcp_servers"):
        names = ", ".join(s["name"] for s in caps["mcp_servers"])
        lines.append(f"MCP servers: {names}")
    if caps.get("extensions_or_plugins"):
        names = ", ".join(f"{p['name']}@{p['version']}" for p in caps["extensions_or_plugins"])
        lines.append(f"Plugins: {names}")
    if caps.get("agents"):
        names = ", ".join(a["name"] for a in caps["agents"])
        lines.append(f"Agents: {names}")
    if len(lines) == 1:
        lines.append("(no extras detected)")
    return "\n".join(lines)


if __name__ == "__main__":
    all_caps = scan_all()
    print(json.dumps(all_caps, indent=2, ensure_ascii=False))
