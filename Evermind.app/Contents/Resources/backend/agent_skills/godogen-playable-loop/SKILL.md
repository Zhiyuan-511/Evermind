GODOGEN PLAYABLE LOOP

Do not build a "game concept". Build a playable loop first.

- Start from one visual target, not a vague style promise: hero silhouette, enemy silhouette, arena landmarks, HUD tone, and lighting direction must feel like one game.
- Define the minimum playable loop: spawn, input, feedback, enemy/objective pressure, fail/win, restart.
- Split work into vertical-slice milestones: start screen, first interaction, combat proof, enemy/objective proof, fail state, polish/tuning.
- Require evidence at each checkpoint: screenshot, browser/gameplay state change, score/health/ammo change, movement proof, or combat proof.
- For premium 3D briefs, "playable first" does not mean graybox forever. The first acceptable slice must already show a readable hero, readable enemies, a believable arena, and a coherent HUD.
- If true 3D models are unavailable, fall back to high-readability procedural silhouettes plus layered materials and decals. Never pretend primitive cubes are "finished modeling".
- Opening combat must be fair: after Start/Restart, the player needs a safe window to move, aim, and fire before unavoidable damage lands. Use spawn grace and/or true radial spawn-distance rules, not only axis-aligned checks.
- Camera-relative controls must pass anti-mirror checks: with the camera behind the player, W moves away from the camera, S toward it, A screen-left, D screen-right; drag right yaws right; upward drag is not inverted unless requested.
- For shooter loops, projectile origin must come from the muzzle / weapon tip or an equivalent forward-offset aim anchor, not the avatar root. Keep visible tracer/trail, muzzle flash, and hit feedback so QA can read combat.
- Tune feel before adding more scope: camera orbit drag, follow smoothing, acceleration/deceleration, look sensitivity, fire cadence, hit feedback, and restart flow.
- Keep systems modular so later passes can swap placeholder assets, rebalance weapons, or improve camera feel without rewriting the whole game.
- Use a repair loop: implement -> capture -> inspect -> fix. Broken visuals, dead input, unreadable enemies, or floaty controls outrank decorative polish.
- If QA cannot actually play the loop, or the screenshots still look placeholder-grade with no recovery path, the build is not approved.
