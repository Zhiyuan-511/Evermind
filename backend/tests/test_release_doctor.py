import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import shutil

import release_doctor


class _DummyRelayManager:
    def __init__(self, relays=None, candidates=None):
        self._relays = relays or []
        self._candidates = candidates or {}

    def list(self):
        return list(self._relays)

    def relay_model_candidates_for(self, model_name: str):
        return list(self._candidates.get(model_name, []))


def _write_backend_tree(base: Path, *, payload: str = "source") -> None:
    for rel_path in release_doctor.CRITICAL_BACKEND_FILES:
        target = base / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{payload}:{rel_path}", encoding="utf-8")


def _write_runtime_vendor(base: Path) -> None:
    for rel_path in release_doctor.RUNTIME_VENDOR_FILES:
        target = base / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rel_path, encoding="utf-8")


def _write_app(app_path: Path, source_backend: Path, *, include_bundle: bool = True) -> None:
    backend_dir = app_path / "Contents" / "Resources" / "backend"
    _write_backend_tree(backend_dir)
    for rel_path in release_doctor.CRITICAL_BACKEND_FILES:
        source_file = source_backend / rel_path
        target_file = backend_dir / rel_path
        if not source_file.exists():
            continue
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(source_file.read_bytes())
    if include_bundle:
        bundle = app_path / "Contents" / "Resources" / "frontend-standalone" / "server.js"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("console.log('bundle');", encoding="utf-8")


class TestReleaseDoctor(unittest.TestCase):
    def test_release_doctor_reports_ready_when_runtime_is_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend_dir = root / "backend"
            frontend_dir = root / "frontend"
            electron_dir = root / "electron" / "dist" / "mac-arm64"
            desktop_root = root / "Desktop"
            settings_file = root / ".evermind" / "config.json"
            settings_hash_file = root / ".evermind" / "config.json.sha256"
            log_file = root / ".evermind" / "logs" / "evermind-backend.log"
            image_workflow = root / "workflows" / "comfyui.json"
            output_dir = root / "output"

            backend_dir.mkdir(parents=True, exist_ok=True)
            frontend_dir.mkdir(parents=True, exist_ok=True)
            (frontend_dir / ".keep").write_text("", encoding="utf-8")
            _write_backend_tree(backend_dir)
            _write_runtime_vendor(backend_dir)
            image_workflow.parent.mkdir(parents=True, exist_ok=True)
            image_workflow.write_text("{}", encoding="utf-8")
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text("{}", encoding="utf-8")
            settings_hash_file.write_text("hash", encoding="utf-8")

            local_app = root / "Evermind.app"
            dist_app = electron_dir / "Evermind.app"
            desktop_app = desktop_root / "Evermind.app"
            _write_app(local_app, backend_dir)
            _write_app(dist_app, backend_dir)
            _write_app(desktop_app, backend_dir)

            settings_data = {
                "api_keys": {"openai": "sk-test"},
                "api_bases": {},
                "default_model": "gpt-5.4",
                "node_model_preferences": {
                    "builder": ["gpt-5.4"],
                    "reviewer": ["gpt-5.4"],
                },
                "output_dir": str(output_dir),
                "qa_enable_browser_use": False,
                "browser_use_python": "",
                "image_generation": {
                    "comfyui_url": "https://assets.example.com",
                    "workflow_template": str(image_workflow),
                },
            }

            fake_paths = {
                "backend_dir": backend_dir,
                "source_root": root,
                "current_app": None,
                "local_app": local_app,
                "dist_app": dist_app,
                "desktop_app": desktop_app,
            }

            env_patch = {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "KIMI_API_KEY": "",
                "QWEN_API_KEY": "",
                "OPENAI_API_BASE": "",
                "ANTHROPIC_API_BASE": "",
                "GEMINI_API_BASE": "",
                "DEEPSEEK_API_BASE": "",
                "KIMI_API_BASE": "",
                "QWEN_API_BASE": "",
            }
            with patch.object(release_doctor, "_detect_project_paths", return_value=fake_paths), \
                 patch.object(release_doctor, "get_relay_manager", return_value=_DummyRelayManager()), \
                 patch.object(release_doctor, "SETTINGS_FILE", settings_file), \
                 patch.object(release_doctor, "SETTINGS_HASH_FILE", settings_hash_file), \
                 patch.object(release_doctor, "LOG_FILE", log_file), \
                 patch.dict(release_doctor.os.environ, env_patch, clear=False):
                report = release_doctor.build_release_doctor_report(
                    settings_data=settings_data,
                    playwright_status={"available": True},
                    current_backend_dir=backend_dir,
                )

            self.assertEqual(report["status"], "ok")
            self.assertTrue(report["ready"])
            self.assertEqual(report["summary"]["fatal"], 0)
            self.assertEqual(report["summary"]["warning"], 0)

    def test_release_doctor_flags_drift_and_missing_model_routes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend_dir = root / "backend"
            electron_dir = root / "electron" / "dist" / "mac-arm64"
            desktop_root = root / "Desktop"
            settings_file = root / ".evermind" / "config.json"
            settings_hash_file = root / ".evermind" / "config.json.sha256"
            log_file = root / ".evermind" / "logs" / "evermind-backend.log"
            output_dir = root / "output"

            backend_dir.mkdir(parents=True, exist_ok=True)
            (root / "frontend").mkdir(parents=True, exist_ok=True)
            _write_backend_tree(backend_dir, payload="src")
            shutil.rmtree(backend_dir / "runtime_vendor", ignore_errors=True)
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text("{}", encoding="utf-8")
            settings_hash_file.write_text("hash", encoding="utf-8")

            local_app = root / "Evermind.app"
            dist_app = electron_dir / "Evermind.app"
            desktop_app = desktop_root / "Evermind.app"
            _write_app(local_app, backend_dir)
            _write_app(dist_app, backend_dir)
            _write_app(desktop_app, backend_dir)
            drift_file = desktop_app / "Contents" / "Resources" / "backend" / "workflow_templates.py"
            drift_file.write_text("drifted", encoding="utf-8")

            settings_data = {
                "api_keys": {},
                "api_bases": {},
                "default_model": "gpt-5.4",
                "node_model_preferences": {
                    "builder": ["gpt-5.4"],
                    "reviewer": ["gpt-5.4"],
                },
                "output_dir": str(output_dir),
                "qa_enable_browser_use": True,
                "browser_use_python": str(root / "missing-venv" / "python"),
                "image_generation": {},
            }

            fake_paths = {
                "backend_dir": backend_dir,
                "source_root": root,
                "current_app": None,
                "local_app": local_app,
                "dist_app": dist_app,
                "desktop_app": desktop_app,
            }

            env_patch = {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "KIMI_API_KEY": "",
                "QWEN_API_KEY": "",
                "OPENAI_API_BASE": "",
                "ANTHROPIC_API_BASE": "",
                "GEMINI_API_BASE": "",
                "DEEPSEEK_API_BASE": "",
                "KIMI_API_BASE": "",
                "QWEN_API_BASE": "",
            }
            with patch.object(release_doctor, "_detect_project_paths", return_value=fake_paths), \
                 patch.object(release_doctor, "get_relay_manager", return_value=_DummyRelayManager()), \
                 patch.object(release_doctor, "SETTINGS_FILE", settings_file), \
                 patch.object(release_doctor, "SETTINGS_HASH_FILE", settings_hash_file), \
                 patch.object(release_doctor, "LOG_FILE", log_file), \
                 patch.dict(release_doctor.os.environ, env_patch, clear=False):
                report = release_doctor.build_release_doctor_report(
                    settings_data=settings_data,
                    playwright_status={"available": False, "reason": "missing chromium"},
                    current_backend_dir=backend_dir,
                )

            issue_codes = {issue["code"] for issue in report["issues"]}
            self.assertEqual(report["status"], "fail")
            self.assertIn("default-model-unavailable", issue_codes)
            self.assertIn("node-model-coverage", issue_codes)
            self.assertIn("runtime-vendor-missing", issue_codes)
            self.assertIn("desktop-app-backend-drift", issue_codes)

    def test_release_doctor_reports_builder_primary_route_through_compatible_gateway(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend_dir = root / "backend"
            frontend_dir = root / "frontend"
            electron_dir = root / "electron" / "dist" / "mac-arm64"
            desktop_root = root / "Desktop"
            settings_file = root / ".evermind" / "config.json"
            settings_hash_file = root / ".evermind" / "config.json.sha256"
            log_file = root / ".evermind" / "logs" / "evermind-backend.log"
            image_workflow = root / "workflows" / "comfyui.json"
            output_dir = root / "output"

            backend_dir.mkdir(parents=True, exist_ok=True)
            frontend_dir.mkdir(parents=True, exist_ok=True)
            (frontend_dir / ".keep").write_text("", encoding="utf-8")
            _write_backend_tree(backend_dir)
            _write_runtime_vendor(backend_dir)
            image_workflow.parent.mkdir(parents=True, exist_ok=True)
            image_workflow.write_text("{}", encoding="utf-8")
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text("{}", encoding="utf-8")
            settings_hash_file.write_text("hash", encoding="utf-8")

            local_app = root / "Evermind.app"
            dist_app = electron_dir / "Evermind.app"
            desktop_app = desktop_root / "Evermind.app"
            _write_app(local_app, backend_dir)
            _write_app(dist_app, backend_dir)
            _write_app(desktop_app, backend_dir)

            settings_data = {
                "api_keys": {"openai": "sk-test", "kimi": "sk-kimi"},
                "api_bases": {"openai": "https://gateway.example/v1"},
                "default_model": "gpt-5.4",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                    "reviewer": ["gpt-5.4"],
                },
                "output_dir": str(output_dir),
                "qa_enable_browser_use": False,
                "browser_use_python": "",
                "image_generation": {
                    "comfyui_url": "https://assets.example.com",
                    "workflow_template": str(image_workflow),
                },
            }

            fake_paths = {
                "backend_dir": backend_dir,
                "source_root": root,
                "current_app": None,
                "local_app": local_app,
                "dist_app": dist_app,
                "desktop_app": desktop_app,
            }

            env_patch = {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "KIMI_API_KEY": "",
                "QWEN_API_KEY": "",
                "OPENAI_API_BASE": "",
                "ANTHROPIC_API_BASE": "",
                "GEMINI_API_BASE": "",
                "DEEPSEEK_API_BASE": "",
                "KIMI_API_BASE": "",
                "QWEN_API_BASE": "",
            }
            with patch.object(release_doctor, "_detect_project_paths", return_value=fake_paths), \
                 patch.object(release_doctor, "get_relay_manager", return_value=_DummyRelayManager()), \
                 patch.object(release_doctor, "SETTINGS_FILE", settings_file), \
                 patch.object(release_doctor, "SETTINGS_HASH_FILE", settings_hash_file), \
                 patch.object(release_doctor, "LOG_FILE", log_file), \
                 patch.dict(release_doctor.os.environ, env_patch, clear=False):
                report = release_doctor.build_release_doctor_report(
                    settings_data=settings_data,
                    playwright_status={"available": True},
                    current_backend_dir=backend_dir,
                )

            self.assertEqual(report["status"], "ok")
            self.assertEqual(
                report["models"]["gpt_5_4_route"]["preferred_route"],
                "compatible_gateway",
            )
            self.assertEqual(
                report["models"]["builder_primary_route"]["preferred_route"],
                "compatible_gateway",
            )
            self.assertEqual(
                report["models"]["builder_viable_chain"],
                ["gpt-5.4", "kimi-coding"],
            )
            self.assertEqual(report["checks"]["builder_fallback_chain"]["status"], "pass")

    def test_release_doctor_warns_when_builder_only_has_single_compatible_gateway_route(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend_dir = root / "backend"
            frontend_dir = root / "frontend"
            electron_dir = root / "electron" / "dist" / "mac-arm64"
            desktop_root = root / "Desktop"
            settings_file = root / ".evermind" / "config.json"
            settings_hash_file = root / ".evermind" / "config.json.sha256"
            log_file = root / ".evermind" / "logs" / "evermind-backend.log"
            output_dir = root / "output"

            backend_dir.mkdir(parents=True, exist_ok=True)
            frontend_dir.mkdir(parents=True, exist_ok=True)
            (frontend_dir / ".keep").write_text("", encoding="utf-8")
            _write_backend_tree(backend_dir)
            _write_runtime_vendor(backend_dir)
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text("{}", encoding="utf-8")
            settings_hash_file.write_text("hash", encoding="utf-8")

            local_app = root / "Evermind.app"
            dist_app = electron_dir / "Evermind.app"
            desktop_app = desktop_root / "Evermind.app"
            _write_app(local_app, backend_dir)
            _write_app(dist_app, backend_dir)
            _write_app(desktop_app, backend_dir)

            settings_data = {
                "api_keys": {"openai": "sk-test"},
                "api_bases": {"openai": "https://gateway.example/v1"},
                "default_model": "gpt-5.4",
                "node_model_preferences": {
                    "builder": ["gpt-5.4"],
                    "reviewer": ["gpt-5.4"],
                },
                "output_dir": str(output_dir),
                "qa_enable_browser_use": False,
                "browser_use_python": "",
                "image_generation": {},
            }

            fake_paths = {
                "backend_dir": backend_dir,
                "source_root": root,
                "current_app": None,
                "local_app": local_app,
                "dist_app": dist_app,
                "desktop_app": desktop_app,
            }

            env_patch = {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "KIMI_API_KEY": "",
                "QWEN_API_KEY": "",
                "OPENAI_API_BASE": "",
                "ANTHROPIC_API_BASE": "",
                "GEMINI_API_BASE": "",
                "DEEPSEEK_API_BASE": "",
                "KIMI_API_BASE": "",
                "QWEN_API_BASE": "",
            }
            with patch.object(release_doctor, "_detect_project_paths", return_value=fake_paths), \
                 patch.object(release_doctor, "get_relay_manager", return_value=_DummyRelayManager()), \
                 patch.object(release_doctor, "SETTINGS_FILE", settings_file), \
                 patch.object(release_doctor, "SETTINGS_HASH_FILE", settings_hash_file), \
                 patch.object(release_doctor, "LOG_FILE", log_file), \
                 patch.dict(release_doctor.os.environ, env_patch, clear=False):
                report = release_doctor.build_release_doctor_report(
                    settings_data=settings_data,
                    playwright_status={"available": True},
                    current_backend_dir=backend_dir,
                )

            issue_codes = {issue["code"] for issue in report["issues"]}
            self.assertEqual(report["status"], "warn")
            self.assertIn("builder-gateway-fallback-thin", issue_codes)

    def test_release_doctor_surfaces_active_gateway_rejection_from_runtime_logs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend_dir = root / "backend"
            frontend_dir = root / "frontend"
            electron_dir = root / "electron" / "dist" / "mac-arm64"
            desktop_root = root / "Desktop"
            settings_file = root / ".evermind" / "config.json"
            settings_hash_file = root / ".evermind" / "config.json.sha256"
            log_file = root / ".evermind" / "logs" / "evermind-backend.log"
            output_dir = root / "output"

            backend_dir.mkdir(parents=True, exist_ok=True)
            frontend_dir.mkdir(parents=True, exist_ok=True)
            (frontend_dir / ".keep").write_text("", encoding="utf-8")
            _write_backend_tree(backend_dir)
            _write_runtime_vendor(backend_dir)
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text("{}", encoding="utf-8")
            settings_hash_file.write_text("hash", encoding="utf-8")
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(
                "\n".join(
                    [
                        "2026-04-03 11:04:32,585 [evermind.ai_bridge] WARNING: Compatible gateway rejection cooldown: provider=openai host=relay cooldown=180s error=Your request was blocked.",
                        "2026-04-03 11:04:32,585 [evermind.ai_bridge] WARNING: Model fallback: node=builder from=gpt-5.4 to=kimi-coding error=Your request was blocked.",
                    ]
                ),
                encoding="utf-8",
            )

            local_app = root / "Evermind.app"
            dist_app = electron_dir / "Evermind.app"
            desktop_app = desktop_root / "Evermind.app"
            _write_app(local_app, backend_dir)
            _write_app(dist_app, backend_dir)
            _write_app(desktop_app, backend_dir)

            settings_data = {
                "api_keys": {"openai": "sk-test", "kimi": "sk-kimi"},
                "api_bases": {"openai": "<your-relay-url>"},
                "default_model": "gpt-5.4",
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                    "reviewer": ["gpt-5.4", "kimi-coding"],
                },
                "output_dir": str(output_dir),
                "qa_enable_browser_use": False,
                "browser_use_python": "",
                "image_generation": {},
            }

            fake_paths = {
                "backend_dir": backend_dir,
                "source_root": root,
                "current_app": None,
                "local_app": local_app,
                "dist_app": dist_app,
                "desktop_app": desktop_app,
            }

            env_patch = {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "KIMI_API_KEY": "",
                "QWEN_API_KEY": "",
                "OPENAI_API_BASE": "",
                "ANTHROPIC_API_BASE": "",
                "GEMINI_API_BASE": "",
                "DEEPSEEK_API_BASE": "",
                "KIMI_API_BASE": "",
                "QWEN_API_BASE": "",
            }
            with patch.object(release_doctor, "_detect_project_paths", return_value=fake_paths), \
                 patch.object(release_doctor, "get_relay_manager", return_value=_DummyRelayManager()), \
                 patch.object(release_doctor, "SETTINGS_FILE", settings_file), \
                 patch.object(release_doctor, "SETTINGS_HASH_FILE", settings_hash_file), \
                 patch.object(release_doctor, "LOG_FILE", log_file), \
                 patch("release_doctor.time.time", return_value=datetime(2026, 4, 3, 11, 5, 32).timestamp()), \
                 patch.dict(release_doctor.os.environ, env_patch, clear=False):
                report = release_doctor.build_release_doctor_report(
                    settings_data=settings_data,
                    playwright_status={"available": True},
                    current_backend_dir=backend_dir,
                )

            issue_codes = {issue["code"] for issue in report["issues"]}
            self.assertEqual(report["status"], "warn")
            self.assertIn("gateway-rejection-cooldown", issue_codes)
            self.assertEqual(
                report["models"]["gpt_5_4_route"]["gateway_health"]["status"],
                "rejection_cooldown",
            )
            self.assertEqual(
                report["models"]["gpt_5_4_route"]["gateway_health"]["fallback"]["to_model"],
                "kimi-coding",
            )


if __name__ == "__main__":
    unittest.main()
