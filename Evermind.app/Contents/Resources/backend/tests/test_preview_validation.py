import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from preview_validation import (
    compare_visual_capture,
    detect_loading_overlay_risk,
    is_bootstrap_html_artifact,
    latest_preview_artifact,
    latest_stable_preview_artifact,
    summarize_vertical_content_gaps,
    summarize_visual_regression,
    validate_html_file,
    validate_preview,
)


def _valid_preview_html(title: str, body_copy: str = "") -> str:
    copy = body_copy or (
        f"{title} preview includes enough structured copy, layout scaffolding, and styling to pass deterministic "
        "validation while remaining small enough for unit tests."
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    :root{{--bg:#0f1420;--fg:#eef2ff;--line:rgba(255,255,255,.12);--accent:#7ce3ff;--panel:#151d2d}}
    *{{box-sizing:border-box}}
    body{{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif;line-height:1.5}}
    header,main,section,footer,nav{{display:block}}
    header{{padding:20px 24px;border-bottom:1px solid var(--line)}}
    nav{{display:flex;gap:12px;flex-wrap:wrap}}
    main{{display:grid;gap:16px;padding:24px}}
    section{{padding:18px;border:1px solid var(--line);border-radius:18px;background:var(--panel)}}
    .stats{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}
    .chip{{display:inline-flex;padding:6px 10px;border:1px solid var(--line);border-radius:999px;color:var(--accent)}}
    @media (max-width: 900px) {{ main{{padding:18px}} .stats{{grid-template-columns:1fr}} }}
  </style>
</head>
<body>
  <header><nav><a href="index.html">Home</a><a href="about.html">About</a><span class="chip">{title}</span></nav></header>
  <main>
    <section><h1>{title}</h1><p>{copy}</p></section>
    <section class="stats"><article><h2>Alpha</h2><p>Structured metric copy.</p></article><article><h2>Beta</h2><p>Secondary layout content.</p></article><article><h2>Gamma</h2><p>Extra density keeps the preview meaningful.</p></article></section>
    <section><p>Supporting narrative ensures the document is not treated as a thin scaffold.</p></section>
    <section><p>Footer-style copy is kept in the main flow for compact unit-test fixtures.</p></section>
  </main>
  <footer><p>{title} footer continuity.</p></footer>
  <script>window.__previewTitle = {title!r};</script>
</body>
</html>"""


class TestLatestPreviewArtifact(unittest.TestCase):
    def test_falls_back_to_root_html_when_no_task_dir(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            root_html = out / "index.html"
            root_html.write_text(_valid_preview_html("Root Preview"), encoding="utf-8")

            task_id, html = latest_preview_artifact(out)
            self.assertEqual(task_id, "root")
            self.assertEqual(html, root_html)

    def test_selects_newest_between_task_and_root(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            task_dir = out / "task_1"
            task_dir.mkdir(parents=True, exist_ok=True)

            task_html = task_dir / "index.html"
            task_html.write_text(_valid_preview_html("Task Preview"), encoding="utf-8")

            root_html = out / "index.html"
            root_html.write_text(_valid_preview_html("Root Preview"), encoding="utf-8")

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

    def test_prefers_root_even_when_task_bucket_is_newer(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            task_dir = out / "task_2"
            task_dir.mkdir(parents=True, exist_ok=True)

            root_html = out / "index.html"
            task_html = task_dir / "index.html"
            root_html.write_text(_valid_preview_html("Root Preview"), encoding="utf-8")
            task_html.write_text(_valid_preview_html("Task Local Preview"), encoding="utf-8")

            now = time.time()
            os.utime(root_html, (now - 30, now - 30))
            os.utime(task_html, (now, now))

            task_id, html = latest_preview_artifact(out)
            self.assertEqual(task_id, "root")
            self.assertEqual(html, root_html)

    def test_skips_invalid_root_preview_and_uses_valid_task_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            root_html = out / "index.html"
            task_dir = out / "task_6"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_html = task_dir / "index.html"
            root_html.write_text(_valid_preview_html("Broken Root Preview"), encoding="utf-8")
            task_html.write_text(_valid_preview_html("Recovered Task Preview"), encoding="utf-8")

            def _validate_side_effect(path: Path):
                return {"ok": path == task_html}

            with patch("preview_validation.validate_html_file", side_effect=_validate_side_effect):
                task_id, html = latest_preview_artifact(out)

            self.assertEqual(task_id, "task_6")
            self.assertEqual(html, task_html)

    def test_prefers_root_index_for_multi_page_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            about_html = out / "about.html"
            index_html.write_text(_valid_preview_html("Home Preview"), encoding="utf-8")
            about_html.write_text(_valid_preview_html("About Preview"), encoding="utf-8")

            now = time.time()
            os.utime(index_html, (now - 10, now - 10))
            os.utime(about_html, (now, now))

            task_id, html = latest_preview_artifact(out)
            self.assertEqual(task_id, "root")
            self.assertEqual(html, index_html)

    def test_ignores_parallel_builder_partial_files(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index_part1.html").write_text("<html><body>part1</body></html>", encoding="utf-8")
            (out / "index_part2.html").write_text("<html><body>part2</body></html>", encoding="utf-8")

            task_id, html = latest_preview_artifact(out)
            self.assertIsNone(task_id)
            self.assertIsNone(html)

    def test_ignores_retry_partial_builder_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "_partial_builder.html").write_text("<html><body>partial</body></html>", encoding="utf-8")
            partial_dir = out / "task_partial-timeout"
            partial_dir.mkdir(parents=True, exist_ok=True)
            (partial_dir / "index.html").write_text("<html><body>partial-dir</body></html>", encoding="utf-8")

            task_id, html = latest_preview_artifact(out)
            self.assertIsNone(task_id)
            self.assertIsNone(html)

    def test_ignores_bootstrap_scaffold_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            bootstrap = out / "index.html"
            bootstrap.write_text(
                "<!doctype html><html><head><meta name=\"evermind-bootstrap\" content=\"pending\"></head>"
                "<body><!-- evermind-bootstrap scaffold --></body></html>",
                encoding="utf-8",
            )

            self.assertTrue(is_bootstrap_html_artifact(bootstrap))
            task_id, html = latest_preview_artifact(out)
            self.assertIsNone(task_id)
            self.assertIsNone(html)

    def test_latest_stable_preview_artifact_prefers_persisted_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_html = out / "_stable_previews" / "run_prev" / "final_success_task_final" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text(_valid_preview_html("Stable Preview"), encoding="utf-8")

            task_id, html = latest_stable_preview_artifact(out)
            self.assertEqual(task_id, "run_prev")
            self.assertEqual(html, stable_html)


class TestLinkedStylesheets(unittest.TestCase):
    def test_validate_html_file_rejects_style_only_black_screen_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html_file = out / "index.html"
            html_file.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN">
<body>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Black Screen</title>
  <style>
    body { margin: 0; background: #000; color: #fff; }
    .hud { position: fixed; top: 0; left: 0; }
  </style>
</body>
</html>""",
                encoding="utf-8",
            )

            report = validate_html_file(html_file)

        self.assertFalse(report.get("ok"))
        self.assertTrue(any("Body lacks meaningful visible content" in err for err in report.get("errors", [])))

    def test_validate_html_file_accepts_local_linked_stylesheet(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html_file = out / "index.html"
            css_file = out / "styles.css"
            css_file.write_text(
                ":root{--bg:#0f1115;--fg:#f7f2ea;--line:rgba(255,255,255,.1)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:Georgia,serif}"
                "header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:20px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            html_file.write_text(
                """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Linked Stylesheet</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header><nav><a href="index.html">Home</a><a href="about.html">About</a></nav></header>
  <main>
    <section class="panel"><h1>Luxury Home</h1><p>Structured landing page with enough real copy to pass deterministic validation while using a shared local stylesheet for the visual system, spacing rhythm, and typography treatment.</p></section>
    <section class="grid"><article class="panel"><h2>Craft</h2><p>Material story with tactile detail, collection framing, and editorial pacing.</p></article><article class="panel"><h2>Service</h2><p>Concierge flow, boutique appointments, and private follow-up moments.</p></article></section>
    <section class="panel"><h2>Collection</h2><p>Additional long-form copy ensures the HTML artifact itself is substantial enough for deterministic validation instead of looking like a tiny scaffold with a linked stylesheet attached.</p></section>
  </main>
  <footer><p>Footer with supporting copy and navigation continuity.</p></footer>
  <script src="app.js"></script>
</body>
</html>""",
                encoding="utf-8",
            )

            report = validate_html_file(html_file)

        self.assertTrue(report.get("ok"))
        self.assertFalse(any("style" in err.lower() for err in report.get("errors", [])))

    def test_validate_html_file_warns_about_missing_local_script_reference(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html_file = out / "index.html"
            html_file.write_text(
                _valid_preview_html(
                    "Missing Script",
                    "This page is otherwise a valid preview artifact, so validation should keep it passable while surfacing the missing local script as a warning rather than treating the whole document as broken.",
                ).replace(
                    "</body>",
                    '  <script src="game.js"></script>\n</body>',
                ),
                encoding="utf-8",
            )

            report = validate_html_file(html_file)

        self.assertTrue(report.get("ok"))
        self.assertTrue(any("Linked local script could not be resolved" in warn for warn in report.get("warnings", [])))

    def test_validate_html_file_rejects_unclosed_inline_script_tag(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html_file = out / "index.html"
            html_file.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Broken Script Arena</title>
  <style>
    body{margin:0;background:#09111a;color:#eef4ff;font-family:system-ui,sans-serif}
    main{display:grid;gap:18px;padding:24px}
    canvas{display:block;width:100%;height:320px;background:#122032;border:1px solid rgba(255,255,255,.12)}
    .hud{display:flex;gap:12px;flex-wrap:wrap}
    @media(max-width:900px){main{padding:18px}}
  </style>
</head>
<body>
  <main>
    <section><h1>Broken Script Arena</h1><p>这段文案足够长，用来确保验证失败是因为脚本结构损坏，而不是因为页面内容太少。</p></section>
    <section><button onclick="startGame()">开始游戏</button><canvas id="game"></canvas><div class="hud"><span>score 0</span><span>health 100</span></div></section>
    <section><p>额外说明文本用于维持页面结构完整度，并模拟一个看起来像成品但实际上脚本被截断的工件。</p></section>
  </main>
  <script>
    function startGame(){
      document.body.dataset.state = 'playing';
      requestAnimationFrame(loop);
    }
    function loop(){
      requestAnimationFrame(loop);
    }
""",
                encoding="utf-8",
            )

            report = validate_html_file(html_file)

        self.assertFalse(report.get("ok"))
        self.assertTrue(any("Missing </script> closing tag" in err for err in report.get("errors", [])))

    def test_validate_html_file_rejects_closed_inline_script_with_invalid_javascript_syntax(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html_file = out / "index.html"
            html_file.write_text(
                """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Broken But Closed Script</title>
  <style>
    body{margin:0;background:#0b1020;color:#eef2ff;font-family:system-ui,sans-serif}
    main{display:grid;gap:18px;padding:24px}
    section{border:1px solid rgba(255,255,255,.1);padding:16px;border-radius:16px}
    @media(max-width:900px){main{padding:18px}}
  </style>
</head>
<body>
  <main>
    <section><h1>Broken But Closed Script</h1><p>This page intentionally closes the script tag but leaves the JavaScript syntax incomplete.</p></section>
    <section><button id="start">Start</button><p>Enough visible content exists so validation must fail because of JavaScript syntax, not blank-page heuristics.</p></section>
    <section><p>Extra content keeps semantic structure and body density above the minimum thresholds.</p></section>
    <section><p>One more section to satisfy deterministic content checks.</p></section>
  </main>
  <script>
    function startGame() {
      const state = { ready: true };
      if (state.ready) {
        document.body.dataset.state = 'playing';
  </script>
</body>
</html>""",
                encoding="utf-8",
            )

            report = validate_html_file(html_file)

        self.assertFalse(report.get("ok"))
        self.assertTrue(any("invalid JavaScript syntax" in err for err in report.get("errors", [])))

    def test_validate_html_file_uses_heuristic_js_check_when_node_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html_file = out / "index.html"
            html_file.write_text(
                """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Heuristic Script Check</title>
  <style>
    body{margin:0;background:#0b1020;color:#eef2ff;font-family:system-ui,sans-serif}
    main{display:grid;gap:18px;padding:24px}
    section{border:1px solid rgba(255,255,255,.1);padding:16px;border-radius:16px}
    @media(max-width:900px){main{padding:18px}}
  </style>
</head>
<body>
  <main>
    <section><h1>Heuristic Script Check</h1><p>Enough visible content exists so the validator must fail on JavaScript integrity rather than blank-page checks.</p></section>
    <section><button id="start">Start</button><p>The inline script below is closed, but it is syntactically truncated.</p></section>
    <section><p>Additional semantic content keeps the test artifact above the structural minimums.</p></section>
    <section><p>One more section keeps the page deterministic for validation.</p></section>
  </main>
  <script>
    function boot() {
      const finalPos = player.position.clone().add(finalPos
    }
  </script>
</body>
</html>""",
                encoding="utf-8",
            )

            with patch("preview_validation._resolve_node_binary", return_value=""):
                report = validate_html_file(html_file)

        self.assertFalse(report.get("ok"))
        self.assertTrue(any("truncated" in err.lower() or "unbalanced" in err.lower() for err in report.get("errors", [])))
        self.assertTrue(any("heuristic JavaScript syntax validation" in warn for warn in report.get("warnings", [])))

    def test_validate_html_file_rejects_undefined_inline_handler_function(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html_file = out / "index.html"
            html_file.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Missing Start Handler</title>
  <style>
    body{margin:0;background:#08111f;color:#eef4ff;font-family:system-ui,sans-serif}
    main{display:grid;gap:18px;padding:24px}
    section{padding:18px;border:1px solid rgba(255,255,255,.12);border-radius:18px;background:rgba(8,17,31,.72)}
    button{padding:12px 18px;border:0;border-radius:999px;background:#22d3ee;color:#04131c;font-weight:700}
    canvas{display:block;width:100%;height:320px;background:#122032;border:1px solid rgba(255,255,255,.12)}
    .hud{display:flex;gap:12px;flex-wrap:wrap}
    .hud span{padding:8px 12px;border-radius:999px;background:rgba(255,255,255,.08)}
    footer{padding:18px 24px;color:#9fb3c8}
    @media(max-width:900px){main{padding:18px}}
  </style>
</head>
<body>
  <main>
    <section><h1>Missing Start Handler</h1><p>这段文案足够长，用来证明页面并不是空壳，而是真正存在启动按钮和 HUD，但入口函数被漏掉了。</p></section>
    <section><button id="startBtn" onclick="startGame()">开始游戏</button><canvas id="game"></canvas><div class="hud"><span>score 0</span><span>health 100</span><span>ammo 30</span></div></section>
    <section><p>额外说明文本用于维持页面完整度，并且让失败原因稳定落在 inline handler 契约缺失，而不是内容质量不足。</p></section>
  </main>
  <footer><p>Footer copy keeps the page materially complete for validation purposes.</p></footer>
  <script>
    const state = { started: false };
    function bootScene(){
      state.started = true;
      requestAnimationFrame(loop);
    }
    function loop(){
      if(!state.started){ return; }
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', () => {});
  </script>
</body>
</html>""",
                encoding="utf-8",
            )

            report = validate_html_file(html_file)

        self.assertFalse(report.get("ok"))
        self.assertTrue(any("undefined function startGame()" in err for err in report.get("errors", [])))

    def test_validate_html_file_accepts_window_defined_inline_handler_function(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html_file = out / "index.html"
            html_file.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Defined Start Handler</title>
  <style>
    body{margin:0;background:#08111f;color:#eef4ff;font-family:system-ui,sans-serif}
    main{display:grid;gap:18px;padding:24px}
    section{padding:18px;border:1px solid rgba(255,255,255,.12);border-radius:18px;background:rgba(8,17,31,.72)}
    button{padding:12px 18px;border:0;border-radius:999px;background:#22d3ee;color:#04131c;font-weight:700}
    canvas{display:block;width:100%;height:320px;background:#122032;border:1px solid rgba(255,255,255,.12)}
    .hud{display:flex;gap:12px;flex-wrap:wrap}
    .hud span{padding:8px 12px;border-radius:999px;background:rgba(255,255,255,.08)}
    footer{padding:18px 24px;color:#9fb3c8}
    @media(max-width:900px){main{padding:18px}}
  </style>
</head>
<body>
  <main>
    <section><h1>Defined Start Handler</h1><p>这段文案足够长，用来证明 inline handler 已经和运行时代码契约对齐，不会因为缺少入口函数而在点击开始时崩掉。</p></section>
    <section><button id="startBtn" onclick="startGame()">开始游戏</button><canvas id="game"></canvas><div class="hud"><span>score 0</span><span>health 100</span><span>ammo 30</span></div></section>
    <section><p>额外说明文本用于维持页面完整度，并避免测试仅仅通过最小空壳。</p></section>
  </main>
  <footer><p>Footer copy keeps the page materially complete for validation purposes.</p></footer>
  <script>
    const state = { started: false };
    window.startGame = function(){
      state.started = true;
      requestAnimationFrame(loop);
    };
    function loop(){
      if(!state.started){ return; }
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', () => {});
  </script>
</body>
</html>""",
                encoding="utf-8",
            )

            report = validate_html_file(html_file)

        self.assertTrue(report.get("ok"))
        self.assertFalse(any("undefined function startGame()" in err for err in report.get("errors", [])))

    def test_validate_html_file_rejects_shared_script_that_breaks_on_sibling_page(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            css_file = out / "styles.css"
            js_file = out / "app.js"
            index_html = out / "index.html"
            contact_html = out / "contact.html"

            css_file.write_text(
                ":root{--bg:#11151c;--fg:#f6f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:Georgia,serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:20px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            js_file.write_text(
                "const overlay = document.querySelector('.page-transition-overlay');\n"
                "overlay.classList.add('active');\n",
                encoding="utf-8",
            )
            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  {overlay}
  <header><nav><a href="index.html">Home</a><a href="contact.html">Contact</a></nav></header>
  <main>
    <section class="panel"><h1>{title}</h1><p>{body}</p></section>
    <section class="grid"><article class="panel"><h2>Collection</h2><p>Editorial product language, service framing, and enough copy density to stay materially complete during deterministic validation.</p></article><article class="panel"><h2>Service</h2><p>Appointment flow, follow-up detail, and real commercial guidance across the route set.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Additional copy ensures the page is substantial enough for validation while a shared script is linked across both pages, and it reinforces that the artifact is a real route instead of a tiny scaffold.</p></section>
  </main>
  <footer><p>Footer continuity for the brand journey.</p></footer>
  <script src="app.js"></script>
</body>
</html>"""
            index_html.write_text(
                page_template.format(
                    title="Home",
                    overlay='<div class="page-transition-overlay"></div>',
                    body="Homepage includes the transition overlay that the shared script expects, but the sibling route does not.",
                ),
                encoding="utf-8",
            )
            contact_html.write_text(
                page_template.format(
                    title="Contact",
                    overlay="",
                    body="Contact route omits the transition overlay, so an unsafe shared script would crash when this page loads.",
                ),
                encoding="utf-8",
            )

            report = validate_html_file(index_html)

        self.assertFalse(report.get("ok"))
        self.assertTrue(any("Shared local script app.js dereferences selector .page-transition-overlay" in err for err in report.get("errors", [])))

    def test_validate_html_file_accepts_guarded_shared_script_across_pages(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            css_file = out / "styles.css"
            js_file = out / "app.js"
            index_html = out / "index.html"
            contact_html = out / "contact.html"

            css_file.write_text(
                ":root{--bg:#11151c;--fg:#f6f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:Georgia,serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:20px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            js_file.write_text(
                "const overlay = document.querySelector('.page-transition-overlay');\n"
                "if (overlay) {\n"
                "  overlay.classList.add('active');\n"
                "}\n",
                encoding="utf-8",
            )
            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  {overlay}
  <header><nav><a href="index.html">Home</a><a href="contact.html">Contact</a></nav></header>
  <main>
    <section class="panel"><h1>{title}</h1><p>{body}</p></section>
    <section class="grid"><article class="panel"><h2>Collection</h2><p>Editorial product language, service framing, and enough copy density to stay materially complete during deterministic validation.</p></article><article class="panel"><h2>Service</h2><p>Appointment flow, follow-up detail, and real commercial guidance across the route set.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Additional copy ensures the page is substantial enough for validation while a shared script is linked across both pages, and it reinforces that the artifact is a real route instead of a tiny scaffold.</p></section>
  </main>
  <footer><p>Footer continuity for the brand journey.</p></footer>
  <script src="app.js"></script>
</body>
</html>"""
            index_html.write_text(
                page_template.format(
                    title="Home",
                    overlay='<div class="page-transition-overlay"></div>',
                    body="Homepage includes the overlay and the shared script now guards the optional element.",
                ),
                encoding="utf-8",
            )
            contact_html.write_text(
                page_template.format(
                    title="Contact",
                    overlay="",
                    body="Contact route still omits the overlay, but the guarded shared script no longer breaks the page.",
                ),
                encoding="utf-8",
            )

            report = validate_html_file(index_html)

        self.assertTrue(report.get("ok"))
        self.assertFalse(any("Shared local script" in err for err in report.get("errors", [])))

    def test_validate_html_file_accepts_create_if_missing_shared_hook(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            css_file = out / "styles.css"
            js_file = out / "app.js"
            index_html = out / "index.html"
            contact_html = out / "contact.html"

            css_file.write_text(
                ":root{--bg:#11151c;--fg:#f6f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:Georgia,serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:20px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            js_file.write_text(
                "function initTransitions() {\n"
                "  let overlay = document.querySelector('.page-transition');\n"
                "  if (!overlay) {\n"
                "    overlay = document.createElement('div');\n"
                "    overlay.className = 'page-transition';\n"
                "    document.body.appendChild(overlay);\n"
                "  }\n"
                "  overlay.classList.add('active');\n"
                "}\n"
                "initTransitions();\n",
                encoding="utf-8",
            )
            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  {overlay}
  <header><nav><a href="index.html">Home</a><a href="contact.html">Contact</a></nav></header>
  <main>
    <section class="panel"><h1>{title}</h1><p>{body}</p></section>
    <section class="grid"><article class="panel"><h2>Collection</h2><p>Editorial product language, service framing, and enough copy density to stay materially complete during deterministic validation.</p></article><article class="panel"><h2>Service</h2><p>Appointment flow, follow-up detail, and real commercial guidance across the route set.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Additional copy ensures the page is substantial enough for validation while a shared script is linked across both pages, and it reinforces that the artifact is a real route instead of a tiny scaffold.</p></section>
  </main>
  <footer><p>Footer continuity for the brand journey.</p></footer>
  <script src="app.js"></script>
</body>
</html>"""
            index_html.write_text(
                page_template.format(
                    title="Home",
                    overlay='<div class="page-transition"></div>',
                    body="Homepage includes the transition node, while the shared script can also create it when a sibling route omits it.",
                ),
                encoding="utf-8",
            )
            contact_html.write_text(
                page_template.format(
                    title="Contact",
                    overlay="",
                    body="Contact route omits the transition node, but the shared script repairs the missing hook before dereferencing it.",
                ),
                encoding="utf-8",
            )

            report = validate_html_file(index_html)

        self.assertTrue(report.get("ok"))
        self.assertFalse(any("Shared local script" in err for err in report.get("errors", [])))

    def test_validate_html_file_accepts_conditional_guard_with_extra_calls(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            css_file = out / "styles.css"
            js_file = out / "app.js"
            index_html = out / "index.html"
            about_html = out / "about.html"

            css_file.write_text(
                ":root{--bg:#11151c;--fg:#f6f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:Georgia,serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:20px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            js_file.write_text(
                "const pageTransition = document.querySelector('.page-transition');\n"
                "document.querySelectorAll('a[href]').forEach((link) => {\n"
                "  const href = link.getAttribute('href');\n"
                "  if (href && !href.startsWith('http') && pageTransition) {\n"
                "    pageTransition.classList.add('active');\n"
                "  }\n"
                "});\n",
                encoding="utf-8",
            )
            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  {overlay}
  <header><nav><a href="index.html">Home</a><a href="about.html">About</a></nav></header>
  <main>
    <section class="panel"><h1>{title}</h1><p>{body}</p></section>
    <section class="grid"><article class="panel"><h2>Collection</h2><p>Editorial product language, service framing, and enough copy density to stay materially complete during deterministic validation.</p></article><article class="panel"><h2>Service</h2><p>Appointment flow, follow-up detail, and real commercial guidance across the route set.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Additional copy ensures the page is substantial enough for validation while a shared script is linked across both pages, and it reinforces that the artifact is a real route instead of a tiny scaffold.</p></section>
  </main>
  <footer><p>Footer continuity for the brand journey.</p></footer>
  <script src="app.js"></script>
</body>
</html>"""
            index_html.write_text(
                page_template.format(
                    title="Home",
                    overlay='<div class="page-transition"></div>',
                    body="Homepage includes the transition node.",
                ),
                encoding="utf-8",
            )
            about_html.write_text(
                page_template.format(
                    title="About",
                    overlay="",
                    body="About route omits the transition node, but the conditional guard prevents runtime failure.",
                ),
                encoding="utf-8",
            )

            report = validate_html_file(index_html)

        self.assertTrue(report.get("ok"))
        self.assertFalse(any("Shared local script" in err for err in report.get("errors", [])))

    def test_validate_html_file_accepts_function_level_early_return_guard(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            css_file = out / "styles.css"
            js_file = out / "app.js"
            index_html = out / "index.html"
            about_html = out / "about.html"

            css_file.write_text(
                ":root{--bg:#11151c;--fg:#f6f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:Georgia,serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:20px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            js_file.write_text(
                "function initHeroParallax() {\n"
                "  const hero = document.getElementById('hero');\n"
                "  if (!hero) return;\n"
                "  const heroContent = hero.querySelector('.hero-content');\n"
                "  hero.querySelectorAll('[data-reveal]').forEach((el) => el.classList.add('ready'));\n"
                "  if (heroContent) {\n"
                "    heroContent.style.opacity = '1';\n"
                "  }\n"
                "}\n"
                "document.addEventListener('DOMContentLoaded', initHeroParallax);\n",
                encoding="utf-8",
            )
            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header><nav><a href="index.html">Home</a><a href="about.html">About</a></nav></header>
  <main>
    {hero_section}
    <section class="grid"><article class="panel"><h2>Collection</h2><p>Editorial product language, service framing, and enough copy density to stay materially complete during deterministic validation.</p></article><article class="panel"><h2>Service</h2><p>Appointment flow, follow-up detail, and real commercial guidance across the route set.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Additional copy ensures the page is substantial enough for validation while a shared script is linked across both pages, and it reinforces that the artifact is a real route instead of a tiny scaffold.</p></section>
  </main>
  <footer><p>Footer continuity for the brand journey.</p></footer>
  <script src="app.js"></script>
</body>
</html>"""
            index_html.write_text(
                page_template.format(
                    title="Home",
                    hero_section='<section id="hero" class="panel"><div class="hero-content"><h1>Home</h1><p>Homepage carries the shared hero section.</p></div><div data-reveal>Reveal</div></section>',
                ),
                encoding="utf-8",
            )
            about_html.write_text(
                page_template.format(
                    title="About",
                    hero_section='<section class="panel"><h1>About</h1><p>This route intentionally omits the hero hook, but the shared script exits early and should remain valid.</p></section>',
                ),
                encoding="utf-8",
            )

            report = validate_html_file(index_html)

        self.assertTrue(report.get("ok"))
        self.assertFalse(any("#hero" in err for err in report.get("errors", [])))

    def test_latest_stable_preview_prefers_snapshot_index(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snapshot_root = out / "_stable_previews" / "run_prev" / "final_success_task_final"
            snapshot_root.mkdir(parents=True, exist_ok=True)
            stable_html = snapshot_root / "index.html"
            about_html = snapshot_root / "about.html"
            stable_html.write_text(_valid_preview_html("Stable Snapshot"), encoding="utf-8")
            about_html.write_text(_valid_preview_html("About Snapshot"), encoding="utf-8")

            now = time.time()
            os.utime(stable_html, (now - 10, now - 10))
            os.utime(about_html, (now, now))

            task_id, html = latest_stable_preview_artifact(out)
            self.assertEqual(task_id, "run_prev")
            self.assertEqual(html, stable_html)


class TestVisualRegressionHelpers(unittest.TestCase):
    def test_summarize_vertical_content_gaps_detects_large_mid_page_void(self):
        result = summarize_vertical_content_gaps(
            [
                {"top": 80, "bottom": 420},
                {"top": 560, "bottom": 840},
                {"top": 1980, "bottom": 2260},
                {"top": 2360, "bottom": 2550},
            ],
            viewport_height=900,
            scroll_height=2800,
        )

        self.assertEqual(result.get("blank_gap_count"), 1)
        self.assertGreater(result.get("largest_blank_gap", 0), 900)

    def test_compare_visual_capture_detects_large_height_shrink(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            baseline = tmp / "baseline.png"
            current = tmp / "current.png"
            diff = tmp / "diff.png"

            base_img = Image.new("RGBA", (1440, 2200), "#0b1020")
            draw = ImageDraw.Draw(base_img)
            draw.rectangle((80, 80, 1360, 560), fill="#f4f7fb")
            draw.rectangle((80, 700, 1360, 2050), fill="#1c2745")
            base_img.save(baseline)

            current_img = Image.new("RGBA", (1440, 1100), "#0b1020")
            draw = ImageDraw.Draw(current_img)
            draw.rectangle((80, 80, 1360, 460), fill="#f4f7fb")
            current_img.save(current)

            result = compare_visual_capture(baseline, current, diff)
            self.assertTrue(diff.exists())

        self.assertTrue(result.get("ok"))
        self.assertLess(result.get("height_change_ratio", 0), -0.35)

    def test_summarize_visual_regression_fails_for_severe_multi_capture_regression(self):
        result = summarize_visual_regression([
            {
                "name": "desktop_full",
                "changed_ratio": 0.58,
                "diff_area_ratio": 0.74,
                "height_change_ratio": -0.52,
                "diff_region": "whole_page",
            },
            {
                "name": "desktop_fold",
                "changed_ratio": 0.49,
                "diff_area_ratio": 0.56,
                "height_change_ratio": 0.0,
                "diff_region": "hero_upper",
            },
        ])

        self.assertEqual(result.get("status"), "fail")
        self.assertTrue(any("shorter" in issue.lower() for issue in result.get("issues", [])))
        self.assertTrue(any("Restore" in suggestion for suggestion in result.get("suggestions", [])))


class TestPreviewValidationSmokeFallback(unittest.TestCase):
    def test_detect_loading_overlay_risk_flags_blocking_spinner(self):
        message = detect_loading_overlay_risk(
            {
                "loading_overlays": [
                    {
                        "label": "loadingOverlay spinner Loading 3D assets...",
                        "coverage_ratio": 0.64,
                        "position": "fixed",
                        "aria_busy": True,
                        "blocking": True,
                    }
                ]
            }
        )

        self.assertIsNotNone(message)
        self.assertIn("loading overlay/spinner", str(message))
        self.assertIn("Loading 3D assets", str(message))

    def test_detect_loading_overlay_risk_ignores_non_blocking_click_to_start_prompt(self):
        message = detect_loading_overlay_risk(
            {
                "loading_overlays": [
                    {
                        "label": "startOverlay Click to start",
                        "coverage_ratio": 0.52,
                        "position": "fixed",
                        "aria_busy": False,
                        "blocking": False,
                    }
                ]
            }
        )

        self.assertIsNone(message)

    def test_smoke_runtime_unavailable_becomes_warning_not_failure(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            html = out / "index.html"
            html.write_text(
                "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'></head><body><main><section><h1>Maison Aurelia</h1><p>Luxury preview</p><button>Explore</button></section></main><script>1</script></body></html>",
                encoding="utf-8",
            )
            with patch("preview_validation.OUTPUT_DIR", out):
                with patch(
                    "preview_validation.validate_html_file",
                    return_value={
                        "ok": True,
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "errors": [],
                        "warnings": [],
                        "checks": {},
                    },
                ):
                    with patch(
                        "preview_validation.run_playwright_smoke",
                        return_value={
                            "status": "skipped",
                            "engine": "playwright",
                            "reason": "playwright runtime unavailable: BrowserType.launch failed",
                        },
                    ):
                        with patch(
                            "preview_validation.run_visual_regression",
                            return_value={"status": "skipped", "summary": "", "issues": [], "suggestions": []},
                        ):
                            result = asyncio.run(validate_preview("http://127.0.0.1:8765/preview/index.html", run_smoke=True))

        self.assertTrue(result.get("ok"))
        self.assertTrue(any("Browser smoke test unavailable" in msg for msg in result.get("warnings", [])))
        self.assertEqual(result.get("smoke", {}).get("status"), "skipped")


if __name__ == "__main__":
    unittest.main()
