import unittest
from unittest.mock import patch

from plugins.base import (
    get_default_plugins_for_node,
    get_image_generation_config,
    is_builder_browser_enabled,
    is_image_generation_available,
    is_qa_computer_use_enabled,
)


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

    def test_qa_computer_use_disabled_by_default(self):
        with patch.dict("os.environ", {"EVERMIND_QA_ENABLE_COMPUTER_USE": "0"}, clear=False):
            defaults = get_default_plugins_for_node("reviewer")
            self.assertNotIn("computer_use", defaults)
            self.assertFalse(is_qa_computer_use_enabled({}))

    def test_qa_computer_use_requires_flag_and_key(self):
        with patch.dict("os.environ", {"EVERMIND_QA_ENABLE_COMPUTER_USE": "1", "OPENAI_API_KEY": "sk-test"}, clear=False):
            defaults = get_default_plugins_for_node("tester")
            self.assertIn("computer_use", defaults)
            self.assertTrue(is_qa_computer_use_enabled({}))

    def test_qa_computer_use_can_be_enabled_from_config(self):
        defaults = get_default_plugins_for_node(
            "reviewer",
            config={"qa_enable_computer_use": True, "openai_api_key": "sk-config"},
        )
        self.assertIn("computer_use", defaults)

    def test_imagegen_defaults_include_comfyui_and_file_ops(self):
        defaults = get_default_plugins_for_node("imagegen")
        self.assertIn("comfyui", defaults)
        self.assertIn("file_ops", defaults)

    def test_image_generation_requires_url_and_workflow(self):
        with patch.dict("os.environ", {}, clear=False):
            self.assertFalse(is_image_generation_available({}))
            self.assertFalse(is_image_generation_available({"image_generation": {"comfyui_url": "http://127.0.0.1:8188"}}))
            self.assertFalse(is_image_generation_available({"image_generation": {"workflow_template": "/tmp/workflow.json"}}))
            self.assertTrue(is_image_generation_available({
                "image_generation": {
                    "comfyui_url": "http://127.0.0.1:8188",
                    "workflow_template": "/tmp/workflow.json",
                }
            }))

    def test_image_generation_config_reads_nested_values(self):
        cfg = get_image_generation_config({
            "image_generation": {
                "comfyui_url": "http://localhost:8188/",
                "workflow_template": "/tmp/asset-workflow.json",
            }
        })
        self.assertEqual(cfg["comfyui_url"], "http://localhost:8188")
        self.assertEqual(cfg["workflow_template"], "/tmp/asset-workflow.json")


if __name__ == "__main__":
    unittest.main()
