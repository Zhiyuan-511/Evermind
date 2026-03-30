SCROLL EVIDENCE CAPTURE

Use this skill for reviewer, tester, or polisher when validating websites, landing pages, dashboards, and multi-section pages.

Required behavior:
- Prefer `browser.record_scroll` so the whole page is captured as continuous evidence.
- If continuous capture is unavailable, manually scroll through the page in steady steps and collect evidence from top, middle, lower, and footer regions.
- For multi-page sites, repeat the scroll evidence capture on every important route, not just the homepage.
- Confirm whether the page height, mid-page sections, sticky navigation, and footer actually render. Fail if the middle collapses, disappears, or stays empty.
- After any click or interaction, verify the changed state with follow-up observation instead of assuming success.

Always report:
- Which pages were visited.
- Whether top, middle, and bottom content all rendered.
- Whether transitions, motion, and interactive elements stayed consistent after scrolling or navigation.
- The exact blocking regressions that should trigger a reject or rework.
