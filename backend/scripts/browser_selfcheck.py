#!/usr/bin/env python3
"""Playwright browser self-check.

Run after:
  python3 -m playwright install chromium
"""

import sys

def main():
    headful = "--headful" in sys.argv
    mode_label = "headful" if headful else "headless"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[失败] playwright not installed. Run: pip install playwright")
        return False

    print(f"[搜索] Launching {mode_label} Chromium...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headful)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            if headful:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            # Local render check (no external network dependency)
            page.set_content(
                "<!doctype html><html><head><title>Selfcheck</title></head>"
                "<body><h1>Playwright Local Check</h1><p>ok</p></body></html>"
            )
            title = page.title()
            print(f"[已配置] Local page rendered: '{title}'")

            # Take a screenshot to verify rendering
            screenshot_path = "/tmp/playwright_selfcheck.png"
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"[已配置] Screenshot saved: {screenshot_path}")

            # Test scroll
            page.mouse.wheel(0, 300)
            page.wait_for_timeout(500)
            print("[已配置] Scroll test passed")

            # Optional network check (best-effort, does not fail whole self-check)
            try:
                page.goto("https://example.com", wait_until="domcontentloaded", timeout=12000)
                print("[已配置] External network check passed (example.com)")
            except Exception as net_err:
                print(f"[警告] External network check skipped/failed: {net_err}")

            browser.close()
            print("[已配置] Browser closed cleanly")
            print("\n🎉 All self-checks passed! Browser engine is ready.")
            return True
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg:
            print("[失败] Chromium not installed yet.")
            print("👉 Run: python3 -m playwright install chromium")
        else:
            print(f"[失败] Self-check failed: {msg}")
        return False

if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
