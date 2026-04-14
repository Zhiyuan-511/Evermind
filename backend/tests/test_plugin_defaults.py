import unittest
from unittest.mock import patch

from plugins.base import (
    get_default_plugins_for_node,
    get_image_generation_config,
    is_builder_browser_enabled,
    is_imagegen_browser_enabled,
    is_qa_browser_use_enabled,
    is_image_generation_available,
    is_polisher_browser_enabled,
    is_qa_computer_use_enabled,
    resolve_enabled_plugins_for_node,
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

    def test_builder_alias_inherits_builder_defaults(self):
        defaults = get_default_plugins_for_node("builder1", config={"builder_enable_browser": True})
        self.assertIn("file_ops", defaults)
        self.assertIn("browser", defaults)

    def test_non_builder_nodes_unchanged(self):
        """Builder browser flag should NOT affect non-builder node defaults."""
        with patch.dict("os.environ", {
            "EVERMIND_BUILDER_ENABLE_BROWSER": "1",
            "EVERMIND_QA_ENABLE_BROWSER_USE": "0",
            "EVERMIND_QA_ENABLE_COMPUTER_USE": "0",
        }):
            defaults = get_default_plugins_for_node("tester")
            # V4.3.1: All pipeline nodes get shell; tester keeps browser-based QA.
            self.assertEqual(defaults, ["file_ops", "shell", "browser"])

    def test_reviewer_defaults_skip_desktop_screenshot_plugin(self):
        with patch.dict("os.environ", {
            "EVERMIND_QA_ENABLE_BROWSER_USE": "0",
            "EVERMIND_QA_ENABLE_COMPUTER_USE": "0",
        }):
            defaults = get_default_plugins_for_node("reviewer")
            self.assertEqual(defaults, ["file_ops", "shell", "browser"])

    def test_reviewer_defaults_strip_browser_when_playwright_runtime_is_unavailable(self):
        defaults = get_default_plugins_for_node(
            "reviewer",
            config={"playwright_available": False},
        )
        self.assertEqual(defaults, ["file_ops", "shell"])

    def test_polisher_defaults_include_browser_and_file_ops(self):
        defaults = get_default_plugins_for_node("polisher")
        self.assertEqual(defaults, ["file_ops", "shell"])

    def test_polisher_browser_enabled_from_env(self):
        with patch.dict("os.environ", {"EVERMIND_POLISHER_ENABLE_BROWSER": "1"}):
            defaults = get_default_plugins_for_node("polisher")
            self.assertEqual(defaults, ["file_ops", "shell", "browser"])

    def test_polisher_browser_enabled_from_runtime_config(self):
        defaults = get_default_plugins_for_node("polisher", config={"polisher_enable_browser": True})
        self.assertEqual(defaults, ["file_ops", "shell", "browser"])
        self.assertTrue(is_polisher_browser_enabled({"polisher_enable_browser": True}))

    def test_polisher_runtime_plugin_resolution_strips_stale_browser(self):
        resolved = resolve_enabled_plugins_for_node(
            "polisher",
            explicit_plugins=["file_ops", "browser"],
            config={"polisher_enable_browser": False},
        )
        self.assertEqual(resolved, ["file_ops"])

    def test_builder_runtime_plugin_resolution_readds_file_ops_when_explicit_plugins_are_bad(self):
        resolved = resolve_enabled_plugins_for_node(
            "builder",
            explicit_plugins=["browser"],
            config={"builder_enable_browser": False},
        )
        self.assertEqual(resolved, ["file_ops"])

    def test_scribe_defaults_include_file_ops_and_shell(self):
        # V4.3.1: All pipeline nodes get file_ops + shell for agentic capability.
        defaults = get_default_plugins_for_node("scribe")
        self.assertEqual(defaults, ["file_ops", "shell"])

    def test_qa_computer_use_disabled_by_default(self):
        with patch.dict("os.environ", {"EVERMIND_QA_ENABLE_COMPUTER_USE": "0"}, clear=False):
            defaults = get_default_plugins_for_node("reviewer")
            self.assertNotIn("computer_use", defaults)
            self.assertFalse(is_qa_computer_use_enabled({}))

    def test_qa_browser_use_disabled_by_default(self):
        with patch.dict("os.environ", {"EVERMIND_QA_ENABLE_BROWSER_USE": "0"}, clear=False):
            defaults = get_default_plugins_for_node("reviewer")
            self.assertNotIn("browser_use", defaults)
            self.assertFalse(is_qa_browser_use_enabled({}))

    def test_qa_browser_use_requires_flag_and_key(self):
        with patch.dict("os.environ", {"EVERMIND_QA_ENABLE_BROWSER_USE": "1", "OPENAI_API_KEY": "sk-test"}, clear=False):
            defaults = get_default_plugins_for_node("tester")
            self.assertIn("browser_use", defaults)
            self.assertTrue(is_qa_browser_use_enabled({}))

    def test_qa_browser_use_can_be_enabled_from_config(self):
        defaults = get_default_plugins_for_node(
            "reviewer",
            config={"qa_enable_browser_use": True, "openai_api_key": "sk-config"},
        )
        self.assertIn("browser_use", defaults)

    def test_qa_browser_use_enabled_from_config_without_key_is_stripped(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            defaults = get_default_plugins_for_node(
                "reviewer",
                config={"qa_enable_browser_use": True},
            )
            self.assertNotIn("browser_use", defaults)

    def test_qa_browser_use_is_stripped_when_runtime_is_unavailable(self):
        defaults = get_default_plugins_for_node(
            "reviewer",
            config={
                "qa_enable_browser_use": True,
                "openai_api_key": "sk-config",
                "playwright_available": False,
                "browser_use_runtime_available": False,
            },
        )
        self.assertEqual(defaults, ["file_ops", "shell"])

    def test_qa_browser_use_runtime_plugin_resolution_strips_when_disabled(self):
        resolved = resolve_enabled_plugins_for_node(
            "reviewer",
            explicit_plugins=["file_ops", "browser_use", "browser"],
            config={"qa_enable_browser_use": False},
        )
        self.assertEqual(resolved, ["file_ops", "browser"])

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

    def test_imagegen_defaults_switch_to_browser_when_backend_is_unavailable(self):
        defaults = get_default_plugins_for_node("imagegen")
        self.assertEqual(defaults, ["file_ops", "source_fetch", "browser"])
        self.assertTrue(is_imagegen_browser_enabled({}))

    def test_imagegen_defaults_include_comfyui_when_backend_is_available(self):
        defaults = get_default_plugins_for_node(
            "imagegen",
            config={
                "image_generation": {
                    "comfyui_url": "http://127.0.0.1:8188",
                    "workflow_template": "/tmp/workflow.json",
                }
            },
        )
        self.assertEqual(defaults, ["file_ops", "comfyui"])

    def test_imagegen_runtime_plugin_resolution_strips_dead_comfyui_and_restores_browser(self):
        resolved = resolve_enabled_plugins_for_node(
            "imagegen",
            explicit_plugins=["file_ops", "comfyui"],
            config={"image_generation": {}},
        )
        self.assertEqual(resolved, ["file_ops", "source_fetch", "browser"])

    def test_imagegen_runtime_plugin_resolution_strips_stale_source_fetch_when_browser_fallback_disabled(self):
        resolved = resolve_enabled_plugins_for_node(
            "imagegen",
            explicit_plugins=["file_ops", "source_fetch", "browser"],
            config={"imagegen_enable_browser": False},
        )
        self.assertEqual(resolved, ["file_ops"])

    def test_spritesheet_defaults_do_not_enable_file_ops(self):
        self.assertEqual(get_default_plugins_for_node("spritesheet"), [])

    def test_assetimport_defaults_do_not_enable_file_ops(self):
        self.assertEqual(get_default_plugins_for_node("assetimport"), [])

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
