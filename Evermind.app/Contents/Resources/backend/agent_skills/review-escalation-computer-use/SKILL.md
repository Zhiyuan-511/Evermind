REVIEW ESCALATION COMPUTER USE

Use browser first. Escalate only when browser evidence is insufficient.

- Preferred path: `browser.snapshot` -> semantic `click`/`fill` -> `wait_for`/`snapshot` verification.
- Stay on the shipped preview surface first. Escalation is for deeper verification, not for switching to a different test target.
- If canvas-heavy UI, desktop chrome, or inaccessible controls block validation, use `computer_use` only if that tool is enabled.
- When escalating, be explicit about the missing evidence and what computer-use must verify.
- Never skip evidence just because one tool struggled.
- If neither browser nor computer_use can prove behavior, reject rather than guess.

Escalation patterns worth copying:
- Use Playwright-like deterministic setup before escalation so the failing step is reproducible.
- Use OpenHands-style desktop escalation only for the blocked interaction itself, then return to evidence-based pass/fail reporting.
