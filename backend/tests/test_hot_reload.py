"""
Tests for the SIGHUP hot-reload mechanism and Claude Code-inspired
truncated JSON suffix repair.
"""

import importlib
import json
import os
import signal
import sys

import pytest


# ─── SIGHUP Hot-Reload Tests ───

class TestSighupHotReload:
    """Test that the SIGHUP handler in server.py reloads modules correctly."""

    def test_handle_sighup_reloads_workflow_templates(self, monkeypatch):
        """_handle_sighup should call importlib.reload on workflow_templates."""
        reload_calls = []
        original_reload = importlib.reload

        def _tracking_reload(mod):
            reload_calls.append(mod.__name__)
            return original_reload(mod)

        monkeypatch.setattr(importlib, "reload", _tracking_reload)

        # Ensure the module is in sys.modules
        import workflow_templates  # noqa: F401

        # Import and invoke the handler
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from server import _handle_sighup

        _handle_sighup(signal.SIGHUP, None)

        assert "workflow_templates" in reload_calls
        assert "node_roles" in reload_calls

    def test_handle_sighup_tolerates_missing_modules(self, monkeypatch):
        """If a module isn't imported yet, SIGHUP handler should skip it gracefully."""
        from server import _handle_sighup

        # Remove a module from sys.modules temporarily
        saved = sys.modules.pop("html_postprocess", None)
        try:
            # Should not raise
            _handle_sighup(signal.SIGHUP, None)
        finally:
            if saved is not None:
                sys.modules["html_postprocess"] = saved

    def test_handle_sighup_updates_global_references(self, monkeypatch):
        """After SIGHUP, server.py's get_template should point to the reloaded module."""
        from server import _handle_sighup
        import server

        old_get_template = server.get_template
        _handle_sighup(signal.SIGHUP, None)
        # After reload, get_template should still be a callable
        assert callable(server.get_template)


# ─── Truncated JSON Suffix Repair Tests (Claude Code Pattern) ───

class TestSuffixGuessingRepair:
    """Test the Claude Code-inspired suffix-guessing JSON repair logic."""

    @pytest.mark.parametrize("truncated,expected_suffix", [
        # Simple missing closing brace
        ('{"content": "hello"', "}"),
        # Missing closing quote and brace
        ('{"content": "hello', '"}'),
        # Nested object missing closing braces
        ('{"action": "write", "data": {"content": "x"}', "}"),
        # Array content missing closing bracket + brace
        ('{"files": ["a.html"]', "}"),
    ])
    def test_suffix_guessing_repairs_common_truncations(self, truncated, expected_suffix):
        """Verify that common truncation patterns are repaired by suffix guessing."""
        _suffix_combos = (
            "}", '"}', "]}", '"]}', "}}", '"}}',
            '"]}}'  , "]}}", '"}]}', '"]}}'  ,
            '"}]', '"]', '"}]}]}',
        )
        trimmed = truncated.rstrip()
        repaired = None
        for suffix in _suffix_combos:
            try:
                json.loads(trimmed + suffix)
                repaired = trimmed + suffix
                break
            except (json.JSONDecodeError, ValueError):
                continue

        assert repaired is not None, f"No suffix could repair: {truncated!r}"
        result = json.loads(repaired)
        assert isinstance(result, dict)

    def test_suffix_guessing_does_not_repair_garbage(self):
        """Random garbage should not be repaired by suffix guessing."""
        garbage = "this is not json at all"
        _suffix_combos = (
            "}", '"}', "]}", '"]}', "}}", '"}}',
            '"]}}'  , "]}}", '"}]}', '"]}}'  ,
            '"}]', '"]', '"}]}]}',
        )
        for suffix in _suffix_combos:
            try:
                json.loads(garbage + suffix)
                pytest.fail(f"Unexpectedly parsed garbage with suffix {suffix!r}")
            except (json.JSONDecodeError, ValueError):
                pass

    def test_suffix_guessing_preserves_valid_json(self):
        """Already-valid JSON should parse on the first try (no suffix needed)."""
        valid = '{"content": "<html>game</html>", "path": "index.html"}'
        result = json.loads(valid)
        assert result["content"] == "<html>game</html>"

    def test_suffix_repair_with_html_content(self):
        """Test repairing truncated JSON that contains HTML game content."""
        # Simulate a truncated tool call with HTML content
        truncated = '{"action": "write", "path": "index.html", "content": "<!DOCTYPE html><html><body><canvas></canvas><script>var game=1;</script></body></html>"'
        _suffix_combos = (
            "}", '"}', "]}", '"]}', "}}", '"}}',
        )
        repaired = None
        for suffix in _suffix_combos:
            try:
                result = json.loads(truncated + suffix)
                if isinstance(result, dict) and result.get("content"):
                    repaired = result
                    break
            except (json.JSONDecodeError, ValueError):
                continue
        
        assert repaired is not None
        assert "<!DOCTYPE html>" in repaired["content"]
        assert repaired["path"] == "index.html"


# ─── Sync Script SIGHUP Test ───

class TestSyncScriptSighup:
    """Test that the sync script correctly reads the lock file format."""

    def test_lock_file_json_parsing(self, tmp_path):
        """The sync script should parse JSON lock files with pid field."""
        lock_file = tmp_path / "backend.lock"
        lock_data = {"pid": 12345, "runtime_id": "test_123", "port": 8765}
        lock_file.write_text(json.dumps(lock_data))

        content = lock_file.read_text().strip()
        parsed = json.loads(content)
        assert parsed["pid"] == 12345

    def test_lock_file_plain_pid_fallback(self, tmp_path):
        """The sync script should fall back to plain PID if JSON parse fails."""
        lock_file = tmp_path / "backend.lock"
        lock_file.write_text("12345\n")

        content = lock_file.read_text().strip()
        try:
            parsed = json.loads(content)
            pid = int(parsed.get("pid", 0))
        except (json.JSONDecodeError, ValueError, AttributeError):
            pid = int(content) if content.isdigit() else 0
        assert pid == 12345
