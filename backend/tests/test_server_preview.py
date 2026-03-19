import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
