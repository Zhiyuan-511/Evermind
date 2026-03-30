import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class TestPreviewListEndpoint(unittest.TestCase):
    def test_preview_list_includes_root_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "index.html").write_text(
                "<!doctype html><html><head></head><body>root</body></html>",
                encoding="utf-8",
            )
            (tmp_out / "styles.css").write_text("body{margin:0;}", encoding="utf-8")

            original_output = server.OUTPUT_DIR
            try:
                server.OUTPUT_DIR = tmp_out
                payload = asyncio.run(server.preview_list())
            finally:
                server.OUTPUT_DIR = original_output

        tasks = payload.get("tasks", [])
        root = next((item for item in tasks if item.get("task_id") == "root"), None)
        self.assertIsNotNone(root)
        self.assertTrue(any(f.get("name") == "index.html" for f in root.get("files", [])))
        self.assertEqual(root.get("preview_url"), "/preview/index.html")

    def test_preview_list_orders_by_latest_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            task_dir = tmp_out / "task_1"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_html = task_dir / "index.html"
            task_html.write_text("<!doctype html><html><head></head><body>task</body></html>", encoding="utf-8")

            root_html = tmp_out / "index.html"
            root_html.write_text("<!doctype html><html><head></head><body>root</body></html>", encoding="utf-8")

            now = time.time()
            older = now - 30
            newer = now
            task_html.touch()
            root_html.touch()
            task_html.chmod(0o644)
            root_html.chmod(0o644)
            # Use os.utime for stable mtime ordering.
            os.utime(task_html, (older, older))
            os.utime(root_html, (newer, newer))

            original_output = server.OUTPUT_DIR
            try:
                server.OUTPUT_DIR = tmp_out
                payload = asyncio.run(server.preview_list())
            finally:
                server.OUTPUT_DIR = original_output

        tasks = payload.get("tasks", [])
        self.assertGreaterEqual(len(tasks), 2)
        self.assertEqual(tasks[0].get("task_id"), "root")

    def test_preview_list_prefers_index_for_root_multi_page_output(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "index.html").write_text(
                "<!doctype html><html><head></head><body>home</body></html>",
                encoding="utf-8",
            )
            (tmp_out / "about.html").write_text(
                "<!doctype html><html><head></head><body>about</body></html>",
                encoding="utf-8",
            )

            original_output = server.OUTPUT_DIR
            try:
                server.OUTPUT_DIR = tmp_out
                payload = asyncio.run(server.preview_list())
            finally:
                server.OUTPUT_DIR = original_output

        tasks = payload.get("tasks", [])
        root = next((item for item in tasks if item.get("task_id") == "root"), None)
        self.assertIsNotNone(root)
        self.assertEqual(root.get("html_file"), "index.html")
        self.assertEqual(root.get("preview_url"), "/preview/index.html")


class TestWorkspaceSync(unittest.TestCase):
    def test_workspace_roots_exposes_runtime_and_delivery_dirs(self):
        with patch.object(server, "load_settings", return_value={
            "workspace": "/path/to/Desktop",
            "artifact_sync_dir": "/tmp/evermind-delivery",
        }):
            payload = asyncio.run(server.workspace_roots())

        self.assertEqual(payload.get("output_dir"), str(server.OUTPUT_DIR))
        self.assertEqual(payload.get("artifact_sync_dir"), "/tmp/evermind-delivery")
        self.assertTrue(any(item.get("kind") == "runtime_output" for item in payload.get("folders", [])))
        self.assertTrue(any(item.get("kind") == "artifact_sync" for item in payload.get("folders", [])))

    def test_workspace_sync_copies_only_deliverables(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            source = tmp_root / "out"
            target = tmp_root / "delivery"
            source.mkdir(parents=True, exist_ok=True)
            (source / "index.html").write_text("<!doctype html><html><body>home</body></html>", encoding="utf-8")
            (source / "about.html").write_text("<!doctype html><html><body>about</body></html>", encoding="utf-8")
            (source / "_partial_builder.html").write_text("<html><body>partial</body></html>", encoding="utf-8")
            task_dir = source / "task_9"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "index.html").write_text("<!doctype html><html><body>fallback</body></html>", encoding="utf-8")

            original_output = server.OUTPUT_DIR
            try:
                server.OUTPUT_DIR = source
                result = server._sync_output_to_target(target)
            finally:
                server.OUTPUT_DIR = original_output
            self.assertTrue(result.get("success"))
            self.assertTrue((target / "index.html").exists())
            self.assertTrue((target / "about.html").exists())
            self.assertFalse((target / "_partial_builder.html").exists())
            self.assertFalse((target / "task_9" / "index.html").exists())
            self.assertTrue((target / server._ARTIFACT_SYNC_MANIFEST).exists())

    def test_live_builder_write_auto_syncs_into_delivery_folder(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            source = tmp_root / "out"
            target = tmp_root / "delivery"
            source.mkdir(parents=True, exist_ok=True)
            written = source / "about.html"
            written.write_text("<!doctype html><html><body>about</body></html>", encoding="utf-8")

            original_output = server.OUTPUT_DIR
            try:
                server.OUTPUT_DIR = source
                with patch.object(server, "load_settings", return_value={"artifact_sync_dir": str(target)}):
                    with patch.object(server, "_is_safe_workspace_root", return_value=True):
                        event = {
                            "type": "subtask_progress",
                            "stage": "builder_write",
                            "path": str(written),
                        }
                        server._maybe_auto_sync_delivery_artifacts(event)
            finally:
                server.OUTPUT_DIR = original_output

            self.assertTrue((target / "about.html").exists())
            self.assertEqual((target / "about.html").read_text(encoding="utf-8"), written.read_text(encoding="utf-8"))
            self.assertEqual(event.get("artifact_sync", {}).get("copied_files"), 1)

    def test_live_polisher_write_auto_syncs_into_delivery_folder(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            source = tmp_root / "out"
            target = tmp_root / "delivery"
            source.mkdir(parents=True, exist_ok=True)
            written = source / "index.html"
            written.write_text("<!doctype html><html><body>polished</body></html>", encoding="utf-8")

            original_output = server.OUTPUT_DIR
            try:
                server.OUTPUT_DIR = source
                with patch.object(server, "load_settings", return_value={"artifact_sync_dir": str(target)}):
                    with patch.object(server, "_is_safe_workspace_root", return_value=True):
                        event = {
                            "type": "subtask_progress",
                            "stage": "artifact_write",
                            "path": str(written),
                            "writer": "polisher",
                        }
                        server._maybe_auto_sync_delivery_artifacts(event)
            finally:
                server.OUTPUT_DIR = original_output

            self.assertTrue((target / "index.html").exists())
            self.assertEqual((target / "index.html").read_text(encoding="utf-8"), written.read_text(encoding="utf-8"))
            self.assertEqual(event.get("artifact_sync", {}).get("copied_files"), 1)

    def test_live_sync_does_not_publish_task_preview_fallback_index(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            source = tmp_root / "out"
            target = tmp_root / "delivery"
            task_dir = source / "task_2"
            task_dir.mkdir(parents=True, exist_ok=True)
            fallback = task_dir / "index.html"
            fallback.write_text("<!doctype html><html><body>fallback</body></html>", encoding="utf-8")
            target.mkdir(parents=True, exist_ok=True)
            existing = target / "index.html"
            existing.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")

            original_output = server.OUTPUT_DIR
            try:
                server.OUTPUT_DIR = source
                with patch.object(server, "load_settings", return_value={"artifact_sync_dir": str(target)}):
                    with patch.object(server, "_is_safe_workspace_root", return_value=True):
                        event = {
                            "type": "files_created",
                            "files": [str(fallback)],
                        }
                        server._maybe_auto_sync_delivery_artifacts(event)
            finally:
                server.OUTPUT_DIR = original_output

            self.assertEqual(existing.read_text(encoding="utf-8"), "<!doctype html><html><body>stable</body></html>")
            self.assertEqual(event.get("artifact_sync", {}).get("copied_files"), 0)

    def test_live_sync_flattens_task_named_pages_into_delivery_root(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            source = tmp_root / "out"
            target = tmp_root / "delivery"
            task_dir = source / "task_7"
            task_dir.mkdir(parents=True, exist_ok=True)
            page = task_dir / "contact.html"
            page.write_text("<!doctype html><html><body>contact</body></html>", encoding="utf-8")

            original_output = server.OUTPUT_DIR
            try:
                server.OUTPUT_DIR = source
                with patch.object(server, "load_settings", return_value={"artifact_sync_dir": str(target)}):
                    with patch.object(server, "_is_safe_workspace_root", return_value=True):
                        event = {
                            "type": "files_created",
                            "files": [str(page)],
                        }
                        server._maybe_auto_sync_delivery_artifacts(event)
            finally:
                server.OUTPUT_DIR = original_output

            self.assertTrue((target / "contact.html").exists())
            self.assertFalse((target / "task_7" / "contact.html").exists())
            self.assertEqual(event.get("artifact_sync", {}).get("copied_files"), 1)


if __name__ == "__main__":
    unittest.main()
