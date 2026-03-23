import os
import tempfile
import time
import unittest
from pathlib import Path

from preview_validation import latest_preview_artifact


class TestLatestPreviewArtifact(unittest.TestCase):
    def test_falls_back_to_root_html_when_no_task_dir(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            root_html = out / "index.html"
            root_html.write_text("<!doctype html><html><head></head><body>root</body></html>", encoding="utf-8")

            task_id, html = latest_preview_artifact(out)
            self.assertEqual(task_id, "root")
            self.assertEqual(html, root_html)

    def test_selects_newest_between_task_and_root(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            task_dir = out / "task_1"
            task_dir.mkdir(parents=True, exist_ok=True)

            task_html = task_dir / "index.html"
            task_html.write_text("<!doctype html><html><head></head><body>task</body></html>", encoding="utf-8")

            root_html = out / "index.html"
            root_html.write_text("<!doctype html><html><head></head><body>root</body></html>", encoding="utf-8")

            now = time.time()
            os_task = now - 10
            os_root = now
            task_html.touch()
            root_html.touch()
            task_html.chmod(0o644)
            root_html.chmod(0o644)
            os.utime(task_html, (os_task, os_task))
            os.utime(root_html, (os_root, os_root))

            task_id, html = latest_preview_artifact(out)
            self.assertEqual(task_id, "root")
            self.assertEqual(html, root_html)

    def test_ignores_parallel_builder_partial_files(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index_part1.html").write_text("<html><body>part1</body></html>", encoding="utf-8")
            (out / "index_part2.html").write_text("<html><body>part2</body></html>", encoding="utf-8")

            task_id, html = latest_preview_artifact(out)
            self.assertIsNone(task_id)
            self.assertIsNone(html)


if __name__ == "__main__":
    unittest.main()
