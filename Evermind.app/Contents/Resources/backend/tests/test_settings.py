import json
import tempfile
import unittest
from pathlib import Path

import settings


class SettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._old_dir = settings.SETTINGS_DIR
        self._old_file = settings.SETTINGS_FILE
        self._old_key_file = settings.SETTINGS_KEY_FILE
        self._old_salt_file = settings.SETTINGS_SALT_FILE
        self._old_hash_file = settings.SETTINGS_HASH_FILE
        self._old_backup_file = settings.SETTINGS_BACKUP_FILE
        settings._cached_cipher = None
        settings._cached_cipher_token = None
        settings._integrity_warning_fingerprints = set()
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        settings.SETTINGS_DIR = base
        settings.SETTINGS_FILE = base / 'config.json'
        settings.SETTINGS_KEY_FILE = base / 'settings.key'
        settings.SETTINGS_SALT_FILE = base / 'settings.salt'
        settings.SETTINGS_HASH_FILE = base / 'config.json.sha256'
        settings.SETTINGS_BACKUP_FILE = base / 'config.json.bak'

    def tearDown(self):
        settings.SETTINGS_DIR = self._old_dir
        settings.SETTINGS_FILE = self._old_file
        settings.SETTINGS_KEY_FILE = self._old_key_file
        settings.SETTINGS_SALT_FILE = self._old_salt_file
        settings.SETTINGS_HASH_FILE = self._old_hash_file
        settings.SETTINGS_BACKUP_FILE = self._old_backup_file
        settings._cached_cipher = None
        settings._cached_cipher_token = None
        settings._integrity_warning_fingerprints = set()
        self._tmp.cleanup()

    def test_save_settings_encrypts_api_keys_and_relays(self):
        sample = settings.load_settings()
        sample['api_keys']['openai'] = 'sk-secret-openai-123456'
        sample['relay_endpoints'] = [{
            'id': 'relay-1',
            'name': 'Relay',
            'base_url': 'https://example.com/v1',
            'api_key': 'relay-secret-xyz',
            'models': ['gpt-4o'],
            'enabled': True,
            'headers': {},
            'max_retries': 2,
            'timeout': 30,
        }]

        self.assertTrue(settings.save_settings(sample))
        raw = settings.SETTINGS_FILE.read_text('utf-8')
        self.assertNotIn('sk-secret-openai-123456', raw)
        self.assertNotIn('relay-secret-xyz', raw)

        payload = json.loads(raw)
        self.assertIn('api_keys_encrypted', payload)
        self.assertTrue(payload['api_keys_encrypted']['openai'].startswith(settings.ENCRYPTED_PREFIX))

        loaded = settings.load_settings()
        self.assertEqual(loaded['api_keys']['openai'], 'sk-secret-openai-123456')
        self.assertEqual(loaded['relay_endpoints'][0]['api_key'], 'relay-secret-xyz')

    def test_deep_merge_dicts_preserves_nested_fields(self):
        base = {
            'privacy': {
                'enabled': True,
                'showIndicator': True,
                'customPatterns': [{'name': 'a'}],
            },
            'control': {'mouseEnabled': True, 'screenCapture': True},
        }
        patch = {
            'privacy': {'enabled': False},
            'control': {'mouseEnabled': False},
        }

        merged = settings.deep_merge_dicts(base, patch)
        self.assertFalse(merged['privacy']['enabled'])
        self.assertTrue(merged['privacy']['showIndicator'])
        self.assertEqual(merged['privacy']['customPatterns'], [{'name': 'a'}])
        self.assertFalse(merged['control']['mouseEnabled'])
        self.assertTrue(merged['control']['screenCapture'])

    def test_default_quality_flags_keep_visible_review_disabled(self):
        loaded = settings.load_settings()
        self.assertFalse(loaded['browser_headful'])
        self.assertFalse(loaded['reviewer_tester_force_headful'])

    def test_default_models_prioritize_kimi_for_heavy_nodes_and_gpt_for_planner(self):
        loaded = settings.load_settings()
        self.assertEqual(loaded['default_model'], 'kimi-coding')
        self.assertEqual(loaded['node_model_preferences']['router'][:2], ['gpt-5.4', 'kimi-coding'])
        self.assertEqual(loaded['node_model_preferences']['planner'][:2], ['gpt-5.4', 'kimi-coding'])
        self.assertEqual(loaded['node_model_preferences']['builder'][:1], ['kimi-coding'])
        self.assertEqual(loaded['node_model_preferences']['polisher'][:1], ['kimi-coding'])
        self.assertEqual(loaded['node_model_preferences']['imagegen'], ['kimi-coding'])
        self.assertEqual(loaded['node_model_preferences']['spritesheet'], ['kimi-coding'])
        self.assertEqual(loaded['node_model_preferences']['assetimport'], ['kimi-coding'])

    def test_node_model_preferences_round_trip(self):
        sample = settings.load_settings()
        sample['node_model_preferences'] = {
            'builder': ['gpt-5.4', 'claude-4-sonnet', 'kimi-coding'],
            'reviewer': ['claude-4-sonnet'],
        }

        self.assertTrue(settings.save_settings(sample))
        loaded = settings.load_settings()

        self.assertEqual(loaded['node_model_preferences']['builder'], ['gpt-5.4', 'claude-4-sonnet', 'kimi-coding'])
        self.assertEqual(loaded['node_model_preferences']['reviewer'], ['claude-4-sonnet'])
        self.assertEqual(loaded['node_model_preferences']['planner'][:2], ['gpt-5.4', 'kimi-coding'])
