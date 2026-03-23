import unittest
from unittest.mock import AsyncMock

from plugins.implementations import BrowserPlugin
from plugins.base import PluginResult


class _FakeLocator:
    def __init__(self, name: str, count: int = 1):
        self.name = name
        self._count = count
        self.clicked = 0
        self.filled = []
        self.pressed = []
        self.focused = 0

    async def count(self):
        return self._count

    def nth(self, _idx: int):
        return self

    async def click(self, timeout: int = 0):
        self.clicked += 1

    async def fill(self, value: str):
        self.filled.append(value)

    async def press(self, key: str):
        self.pressed.append(key)

    async def focus(self):
        self.focused += 1

    async def wait_for(self, state: str = "visible", timeout: int = 0):
        return None


class _FakePage:
    def __init__(self, selectors=None, labels=None, placeholders=None, roles=None, texts=None):
        self._selectors = selectors or {}
        self._labels = labels or {}
        self._placeholders = placeholders or {}
        self._roles = roles or {}
        self._texts = texts or {}
        self.url = "http://127.0.0.1:8765/preview/"
        self.keyboard = type("Keyboard", (), {"press": AsyncMock()})()
        self.mouse = type("Mouse", (), {"wheel": AsyncMock()})()

    def locator(self, selector: str):
        return _FakeLocator(f"selector:{selector}", self._selectors.get(selector, 0))

    def get_by_label(self, label: str, exact: bool = False):
        return _FakeLocator(f"label:{label}:{exact}", self._labels.get((label, exact), self._labels.get(label, 0)))

    def get_by_placeholder(self, placeholder: str, exact: bool = False):
        return _FakeLocator(
            f"placeholder:{placeholder}:{exact}",
            self._placeholders.get((placeholder, exact), self._placeholders.get(placeholder, 0)),
        )

    def get_by_role(self, role: str, name=None, exact: bool = False):
        key = (role, name, exact)
        fallback_key = (role, name)
        bare_key = role
        return _FakeLocator(
            f"role:{role}:{name}:{exact}",
            self._roles.get(key, self._roles.get(fallback_key, self._roles.get(bare_key, 0))),
        )

    def get_by_text(self, text: str, exact: bool = False):
        return _FakeLocator(f"text:{text}:{exact}", self._texts.get((text, exact), self._texts.get(text, 0)))

    async def wait_for_timeout(self, _ms: int):
        return None

    async def wait_for_load_state(self, _state: str, timeout: int = 0):
        return None

    async def wait_for_url(self, _url: str, timeout: int = 0):
        return None

    async def text_content(self, _selector: str):
        return "body text"

    async def evaluate(self, _script: str):
        return []


class TestBrowserPluginRefResolution(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_locator_uses_snapshot_ref(self):
        plugin = BrowserPlugin()
        plugin._page_snapshot = AsyncMock(return_value={
            "interactive": [
                {"ref": "ref-1", "selector": "button.primary", "role": "button", "text": "Play"},
            ]
        })
        page = _FakePage(selectors={"button.primary": 1})

        locator, target = await plugin._resolve_locator(page, {"ref": "ref-1"})

        self.assertIsNotNone(locator)
        self.assertEqual(target, "ref=ref-1 -> button.primary")

    async def test_resolve_locator_falls_back_when_ref_missing(self):
        plugin = BrowserPlugin()
        plugin._page_snapshot = AsyncMock(return_value={"interactive": []})
        page = _FakePage(selectors={"#submit": 1})

        locator, target = await plugin._resolve_locator(page, {"ref": "ref-99", "selector": "#submit"})

        self.assertIsNotNone(locator)
        self.assertEqual(target, "#submit")

    async def test_resolve_target_hint_matches_snapshot_semantically(self):
        plugin = BrowserPlugin()
        plugin._page_snapshot = AsyncMock(return_value={
            "interactive": [
                {"ref": "ref-1", "selector": "button.primary", "role": "button", "text": "Start Game"},
                {"ref": "ref-2", "selector": "input[name='email']", "role": "textbox", "placeholder": "Email"},
            ]
        })
        page = _FakePage(selectors={"button.primary": 1})

        locator, descriptor, item = await plugin._resolve_target_hint(page, "start game", intent="click")

        self.assertIsNotNone(locator)
        self.assertIn("ref-1", descriptor)
        self.assertEqual(item["ref"], "ref-1")

    async def test_execute_act_click_uses_target_hint_and_reports_subaction(self):
        plugin = BrowserPlugin()
        page = _FakePage(selectors={"button.primary": 1})
        plugin._ensure_browser = AsyncMock(return_value=page)
        plugin._page_snapshot = AsyncMock(return_value={
            "interactive": [
                {"ref": "ref-1", "selector": "button.primary", "role": "button", "text": "Start Game"},
            ]
        })
        plugin._finalize_browser_result = AsyncMock(return_value=PluginResult(success=True, data={"ok": True}))

        result = await plugin.execute({"action": "act", "intent": "click", "target": "Start Game"})

        self.assertTrue(result.success)
        kwargs = plugin._finalize_browser_result.await_args.kwargs
        self.assertEqual(kwargs["action"], "act")
        self.assertEqual(kwargs["base_data"]["subaction"], "click")
        self.assertEqual(kwargs["base_data"]["matched_ref"], "ref-1")

    async def test_execute_observe_builds_observation_summary(self):
        plugin = BrowserPlugin()
        page = _FakePage()
        plugin._ensure_browser = AsyncMock(return_value=page)
        plugin._page_snapshot = AsyncMock(return_value={
            "title": "Demo",
            "body_text": "Start game with keyboard controls",
            "interactive": [{"ref": "ref-1", "text": "Start Game", "role": "button"}],
            "counts": {"buttons": 1, "links": 0, "inputs": 0, "forms": 0, "canvas": 1},
        })
        plugin._finalize_browser_result = AsyncMock(return_value=PluginResult(success=True, data={"ok": True}))

        result = await plugin.execute({"action": "observe", "goal": "inspect start flow"})

        self.assertTrue(result.success)
        kwargs = plugin._finalize_browser_result.await_args.kwargs
        self.assertEqual(kwargs["action"], "observe")
        self.assertIn("Best refs", kwargs["base_data"]["observation"])


if __name__ == "__main__":
    unittest.main()
