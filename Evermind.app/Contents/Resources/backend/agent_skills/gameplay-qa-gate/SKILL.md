GAMEPLAY QA GATE

The product is not "tested" unless real gameplay happened.

- Use the shipped Evermind preview / in-app browser surface first. Do not approve a game just because an unrelated external browser page loaded.
- Find the start/play control from `browser.snapshot`, then click it by visible text or role.
- Use `press_sequence` with multiple gameplay keys, not a single token press.
- Keep interaction going long enough to produce a visible state change.
- After gameplay input, collect a second `snapshot` or screenshot and confirm:
  - the screen changed from the menu/start state
  - score / HUD / player position / scene content changed
  - no fatal browser runtime errors appeared

Shooter-specific checks:
- Verify keyboard movement respects the camera frame: with the camera behind the player, A must move screen-left and D must move screen-right.
- Verify drag-look / pointer-look really rotates the TPS camera in the intended direction.
- Verify upward drag does not behave like inverted look unless the brief explicitly asked for inverted pitch.
- Fire multiple shots and confirm the projectile starts from a muzzle-aligned origin, not the avatar root.
- Confirm there is a visible tracer / projectile core, muzzle flash or firing cue, and impact feedback.
- If gunfire is invisible, backwards, body-centered, or disconnected from the crosshair/aim vector, reject.

Reference discipline:
- Favor repeatable browser flows and evidence capture in the spirit of Playwright-style QA.
- When visual fidelity is borderline, use screenshot-before/after discipline similar to reg-suit / BackstopJS instead of guessing.

REJECT if any of these are true:
- start screen never transitions into gameplay
- keyboard input does not change the visible state
- the product logs runtime errors or appears frozen
- the evidence is only a title screen, menu, or static first frame
