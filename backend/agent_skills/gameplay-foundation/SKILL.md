GAMEPLAY FOUNDATION

For browser games, prioritize interaction reliability over visual ambition.

- Start screen must have a clearly clickable start/play button.
- Keyboard listeners belong on `document`, and gameplay surface should receive focus after start.
- Show a visible HUD: score, health, timer, or progress.
- Every player input should create immediate feedback: movement, particles, sound, score, or state change.
- Provide a real fail/win/reset loop. A game without a complete loop feels broken.
- If using canvas, redraw every frame and keep the first 10 seconds visually dynamic.

Fail-safe rule:
- If time is tight, simplify mechanics, but never ship a game that starts but cannot be controlled.
