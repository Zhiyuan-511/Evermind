GAMEPLAY QA GATE

The product is not "tested" unless real gameplay happened.

- Find the start/play control from `browser.snapshot`, then click it by visible text or role.
- Use `press_sequence` with multiple gameplay keys, not a single token press.
- Keep interaction going long enough to produce a visible state change.
- After gameplay input, collect a second `snapshot` or screenshot and confirm:
  - the screen changed from the menu/start state
  - score / HUD / player position / scene content changed
  - no fatal browser runtime errors appeared

REJECT if any of these are true:
- start screen never transitions into gameplay
- keyboard input does not change the visible state
- the product logs runtime errors or appears frozen
- the evidence is only a title screen, menu, or static first frame
