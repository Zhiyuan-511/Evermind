import unittest
from unittest.mock import patch

from plugins.base import get_default_plugins_for_node, is_builder_browser_enabled


class TestBuilderBrowserDefaults(unittest.TestCase):
    def test_builder_browser_disabled_by_default(self):
        with patch.dict("os.environ", {"EVERMIND_BUILDER_ENABLE_BROWSER": "0"}):
            defaults = get_default_plugins_for_node("builder")
            self.assertIn("file_ops", defaults)
            self.assertNotIn("browser", defaults)

    def test_builder_browser_enabled_from_env(self):
        with patch.dict("os.environ", {"EVERMIND_BUILDER_ENABLE_BROWSER": "1"}):
            defaults = get_default_plugins_for_node("builder")
            self.assertIn("file_ops", defaults)
            self.assertIn("browser", defaults)

    def test_builder_browser_enabled_from_runtime_config(self):
        defaults = get_default_plugins_for_node("builder", config={"builder_enable_browser": True})
        self.assertIn("browser", defaults)
        self.assertTrue(is_builder_browser_enabled({"builder_enable_browser": True}))

    def test_non_builder_nodes_unchanged(self):
        """Builder browser flag should NOT affect non-builder node defaults."""
        with patch.dict("os.environ", {"EVERMIND_BUILDER_ENABLE_BROWSER": "1"}):
            defaults = get_default_plugins_for_node("tester")
            # Tester now has screenshot+browser for visual testing (always, not flag-dependent)
            self.assertEqual(defaults, ["file_ops", "screenshot", "browser"])


if __name__ == "__main__":
    unittest.main()
