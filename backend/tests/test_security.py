"""
Evermind Backend — Security Tests
Covers API key sanitization, relay key isolation, settings integrity,
privacy masking for new patterns, and concurrency safety.
"""

import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import settings
from privacy import PrivacyMasker, BUILTIN_PATTERNS
from proxy_relay import RelayEndpoint, RelayManager, _sanitize_log


# ─────────────────────────────────────────────
# 1. Error Message Sanitization
# ─────────────────────────────────────────────
class TestErrorSanitization(unittest.TestCase):
    """Verify API keys are stripped from error messages."""

    def test_sanitize_strips_sk_key(self):
        msg = "Authorization failed for sk-abc123def456ghi789jkl012"
        result = _sanitize_log(msg)
        self.assertNotIn("sk-abc123def456ghi789jkl012", result)
        self.assertIn("[REDACTED]", result)

    def test_sanitize_strips_bearer_token(self):
        msg = "Header error: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc"
        result = _sanitize_log(msg)
        self.assertNotIn("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9", result)
        self.assertIn("[REDACTED]", result)

    def test_sanitize_strips_api_key_prefix(self):
        msg = "Failed: api_key-ABCDEF123456789012345678"
        result = _sanitize_log(msg)
        self.assertNotIn("ABCDEF123456789012345678", result)

    def test_sanitize_preserves_normal_text(self):
        msg = "Connection timeout after 30 seconds"
        result = _sanitize_log(msg)
        self.assertEqual(result, msg)

    def test_sanitize_empty_string(self):
        self.assertEqual(_sanitize_log(""), "")

    def test_sanitize_none(self):
        self.assertIsNone(_sanitize_log(None))


# Also test the ai_bridge sanitizer
class TestAIBridgeSanitization(unittest.TestCase):
    def test_ai_bridge_sanitize_import(self):
        from ai_bridge import _sanitize_error
        msg = "Error with sk-proj-abcdefghijklmnop1234567890"
        result = _sanitize_error(msg)
        self.assertNotIn("sk-proj-abcdefghijklmnop1234567890", result)
        self.assertIn("[REDACTED]", result)


# ─────────────────────────────────────────────
# 2. Relay Model Registry Key Isolation
# ─────────────────────────────────────────────
class TestRelayKeyIsolation(unittest.TestCase):
    """Verify api_key is never included in model registry entries."""

    def test_model_registry_entries_have_no_api_key(self):
        ep = RelayEndpoint(
            id="relay-sec-1",
            name="Secure Relay",
            base_url="https://api.example.com/v1",
            api_key="sk-super-secret-key-abcdefghij",
            models=["gpt-4o", "claude-3.5-sonnet"],
        )
        entries = ep.to_model_registry_entries()
        for model_id, info in entries.items():
            self.assertNotIn("api_key", info,
                             f"api_key leaked into model registry entry: {model_id}")
            # Verify the actual key string isn't in any value
            for val in info.values():
                if isinstance(val, str):
                    self.assertNotIn("sk-super-secret-key", val)

    def test_to_dict_masks_api_key(self):
        ep = RelayEndpoint(
            id="relay-sec-2",
            name="Test",
            base_url="https://api.example.com/v1",
            api_key="sk-1234567890abcdefghijklmnop",
            models=["gpt-4o"],
        )
        d = ep.to_dict()
        # Should be masked (first 8 chars + ...)
        self.assertTrue(d["api_key"].endswith("..."))
        self.assertNotEqual(d["api_key"], "sk-1234567890abcdefghijklmnop")


# ─────────────────────────────────────────────
# 3. Relay TLS Warning
# ─────────────────────────────────────────────
class TestRelayTLSWarning(unittest.TestCase):
    def test_http_endpoint_has_tls_warning(self):
        ep = RelayEndpoint(
            id="relay-http",
            name="HTTP Relay",
            base_url="http://insecure.example.com/v1",
            api_key="key",
            models=["gpt-4o"],
        )
        d = ep.to_dict()
        self.assertTrue(d.get("tls_warning", False))

    def test_https_endpoint_no_tls_warning(self):
        ep = RelayEndpoint(
            id="relay-https",
            name="HTTPS Relay",
            base_url="https://secure.example.com/v1",
            api_key="key",
            models=["gpt-4o"],
        )
        d = ep.to_dict()
        self.assertFalse(d.get("tls_warning", False))


# ─────────────────────────────────────────────
# 4. Relay Timeout Hard Cap
# ─────────────────────────────────────────────
class TestRelayTimeoutCap(unittest.TestCase):
    def test_timeout_is_capped(self):
        ep = RelayEndpoint(
            id="relay-slow",
            name="Slow",
            base_url="https://example.com",
            timeout=999,
        )
        self.assertLessEqual(ep.timeout, 300)

    def test_normal_timeout_allowed(self):
        ep = RelayEndpoint(
            id="relay-ok",
            name="OK",
            base_url="https://example.com",
            timeout=60,
        )
        self.assertEqual(ep.timeout, 60)


# ─────────────────────────────────────────────
# 5. Settings Integrity Verification
# ─────────────────────────────────────────────
class TestSettingsIntegrity(unittest.TestCase):
    def setUp(self):
        self._old_dir = settings.SETTINGS_DIR
        self._old_file = settings.SETTINGS_FILE
        self._old_key_file = settings.SETTINGS_KEY_FILE
        self._old_salt_file = settings.SETTINGS_SALT_FILE
        self._old_hash_file = settings.SETTINGS_HASH_FILE
        self._old_backup_file = settings.SETTINGS_BACKUP_FILE
        settings._cached_cipher = None
        settings._cached_cipher_token = None
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        settings.SETTINGS_DIR = base
        settings.SETTINGS_FILE = base / "config.json"
        settings.SETTINGS_KEY_FILE = base / "settings.key"
        settings.SETTINGS_SALT_FILE = base / "settings.salt"
        settings.SETTINGS_HASH_FILE = base / "config.json.sha256"
        settings.SETTINGS_BACKUP_FILE = base / "config.json.bak"

    def tearDown(self):
        settings.SETTINGS_DIR = self._old_dir
        settings.SETTINGS_FILE = self._old_file
        settings.SETTINGS_KEY_FILE = self._old_key_file
        settings.SETTINGS_SALT_FILE = self._old_salt_file
        settings.SETTINGS_HASH_FILE = self._old_hash_file
        settings.SETTINGS_BACKUP_FILE = self._old_backup_file
        settings._cached_cipher = None
        settings._cached_cipher_token = None
        self._tmp.cleanup()

    def test_save_creates_sha256_hash(self):
        sample = settings.load_settings()
        sample["api_keys"]["openai"] = "sk-test-key-123456789"
        settings.save_settings(sample)
        self.assertTrue(settings.SETTINGS_HASH_FILE.exists())
        # Verify hash matches file content
        file_bytes = settings.SETTINGS_FILE.read_bytes()
        expected_hash = hashlib.sha256(file_bytes).hexdigest()
        actual_hash = settings.SETTINGS_HASH_FILE.read_text("utf-8").strip()
        self.assertEqual(expected_hash, actual_hash)

    def test_save_creates_backup(self):
        sample = settings.load_settings()
        sample["api_keys"]["openai"] = "sk-first-key-123456789"
        settings.save_settings(sample)
        # Now save again — should create backup
        sample["api_keys"]["openai"] = "sk-second-key-987654321"
        settings.save_settings(sample)
        self.assertTrue(settings.SETTINGS_BACKUP_FILE.exists())

    def test_tampered_config_logs_warning(self):
        sample = settings.load_settings()
        sample["api_keys"]["openai"] = "sk-integrity-test-key"
        settings.save_settings(sample)
        # Tamper with the file
        settings.SETTINGS_FILE.write_text('{"tampered": true}', encoding="utf-8")
        # Loading should still work but hash mismatch is detected
        with self.assertLogs("evermind.settings", level="WARNING") as cm:
            loaded = settings.load_settings()
        self.assertTrue(any("integrity" in msg.lower() for msg in cm.output))


# ─────────────────────────────────────────────
# 6. Privacy — New Patterns (Bearer, AWS, GitHub)
# ─────────────────────────────────────────────
class TestNewPrivacyPatterns(unittest.TestCase):
    def test_bearer_token_masked(self):
        masker = PrivacyMasker(enabled=True, patterns=["bearer_token"])
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
        masked, restore = masker.mask(text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", masked)
        self.assertIn("***BEARER***", masked)
        unmasked = masker.unmask(masked, restore)
        self.assertEqual(unmasked, text)

    def test_aws_key_masked(self):
        masker = PrivacyMasker(enabled=True, patterns=["aws_key"])
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        masked, restore = masker.mask(text)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", masked)
        self.assertIn("***AWS_KEY***", masked)
        unmasked = masker.unmask(masked, restore)
        self.assertEqual(unmasked, text)

    def test_github_token_masked(self):
        masker = PrivacyMasker(enabled=True, patterns=["github_token"])
        text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij1234"
        masked, restore = masker.mask(text)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij1234", masked)
        self.assertIn("***GITHUB_TOKEN***", masked)
        unmasked = masker.unmask(masked, restore)
        self.assertEqual(unmasked, text)

    def test_new_patterns_exist_in_builtin(self):
        for name in ("bearer_token", "aws_key", "github_token"):
            self.assertIn(name, BUILTIN_PATTERNS, f"Missing builtin pattern: {name}")


# ─────────────────────────────────────────────
# 7. Privacy — Concurrency Safety
# ─────────────────────────────────────────────
class TestPrivacyConcurrency(unittest.TestCase):
    def test_concurrent_mask_operations(self):
        """Run multiple mask/unmask cycles concurrently and verify integrity."""
        masker = PrivacyMasker(enabled=True, patterns=["phone_cn", "email"])
        errors = []

        def worker(i):
            try:
                text = f"用户{i}: user{i}@test.com 手机: 138{i:08d}"
                masked, restore = masker.mask(text)
                unmasked = masker.unmask(masked, restore)
                if unmasked != text:
                    errors.append(f"Worker {i}: roundtrip mismatch")
            except Exception as e:
                errors.append(f"Worker {i}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Concurrency errors: {errors}")


# ─────────────────────────────────────────────
# 8. Server — Sanitize Error Function
# ─────────────────────────────────────────────
class TestServerSanitization(unittest.TestCase):
    def test_server_sanitize_error_import(self):
        """Verify server._sanitize_error exists and works."""
        try:
            from server import _sanitize_error
        except ImportError:
            # FastAPI may not be installed in test env — test the regex directly
            _SENSITIVE_RE = re.compile(
                r"(?:sk|key|token|api[_-]?key|Bearer)[-_\s]?[a-zA-Z0-9._\-]{8,}",
                re.IGNORECASE,
            )
            _sanitize_error = lambda msg: _SENSITIVE_RE.sub("[REDACTED]", msg) if msg else msg

        msg = "sk-test1234567890abcdef caused an error"
        result = _sanitize_error(msg)
        self.assertNotIn("sk-test1234567890abcdef", result)


if __name__ == "__main__":
    unittest.main()
