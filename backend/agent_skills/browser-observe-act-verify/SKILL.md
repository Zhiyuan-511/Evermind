BROWSER TEST LOOP

Use an observe -> act -> verify sequence, not blind selector guessing.

1. Observe first:
- Navigate to the page.
- Call `browser.snapshot` to inspect visible buttons, links, inputs, canvas count, and body text.

2. Act with semantic targets:
- Prefer `click`/`fill` by `text`, `role`, `label`, or `placeholder`.
- Use raw CSS selectors only when semantic targeting fails.
- After each meaningful action, call `wait_for` or `snapshot` instead of immediately declaring success.
- An approval is invalid if post-action verification is missing.

3. Verify state changed:
- Confirm the page `state_hash` changes after interaction when behavior should change.
- Reject if only the initial screen was observed and no post-action state was verified.
- Reject if browser diagnostics show console/page runtime errors.

4. Leave an evidence trail:
- Mention what you clicked, what changed, and what remained broken.
- Prefer a before/after interaction ledger instead of vague statements like "tested successfully".
