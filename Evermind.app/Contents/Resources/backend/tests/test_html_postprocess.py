import unittest

from html_postprocess import postprocess_html, postprocess_javascript, postprocess_stylesheet


class HtmlPostprocessTests(unittest.TestCase):
    def test_postprocess_javascript_guards_optional_shared_hooks(self):
        source = (
            "const nav = document.getElementById('nav');\n"
            "const overlay = document.querySelector('.page-transition-overlay');\n"
            "nav.classList.add('scrolled');\n"
            "overlay.classList.add('active');\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("if (nav) nav.classList.add('scrolled');", result)
        self.assertIn("if (overlay) overlay.classList.add('active');", result)

    def test_postprocess_javascript_normalizes_page_transition_selector_variants(self):
        source = (
            "const overlay = document.querySelector('.page-transition');\n"
            "overlay.classList.add('active');\n"
        )

        result = postprocess_javascript(source)

        self.assertIn(
            "document.querySelector('.page-transition, .page-transition-overlay')",
            result,
        )
        self.assertIn("if (overlay) overlay.classList.add('active');", result)

    def test_postprocess_html_adds_reverse_page_transition_alias(self):
        source = (
            "<!DOCTYPE html><html><head><title>X</title></head>"
            "<body><div class=\"page-transition-overlay\"></div></body></html>"
        )

        result = postprocess_html(source)

        self.assertIn('class="page-transition-overlay page-transition"', result)

    def test_postprocess_html_strips_remote_font_links(self):
        source = (
            "<!DOCTYPE html><html><head>"
            "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
            "<link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap\" rel=\"stylesheet\">"
            "</head><body><main>ok</main></body></html>"
        )

        result = postprocess_html(source)

        self.assertNotIn("fonts.googleapis.com", result)
        self.assertIn("<main>ok</main>", result)

    def test_postprocess_html_repairs_head_body_order_when_body_starts_inside_head(self):
        source = (
            "<!DOCTYPE html><html><head><title>X</title><style>body{margin:0}"
            "<body><main>ok</main></html>"
        )

        result = postprocess_html(source)

        self.assertIn("</head>", result.lower())
        self.assertIn("</style>", result.lower())
        self.assertTrue(result.lower().index("</head>") < result.lower().index("<body"))
        self.assertIn("</body>", result.lower())

    def test_postprocess_stylesheet_strips_remote_font_imports(self):
        source = (
            "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap');\n"
            "body { font-family: 'Inter', sans-serif; }\n"
        )

        result = postprocess_stylesheet(source)

        self.assertNotIn("fonts.googleapis.com", result)
        self.assertIn("font-family", result)


if __name__ == '__main__':
    unittest.main()
