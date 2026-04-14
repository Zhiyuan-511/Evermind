GODOGEN TPS CONTROL SANITY LOCK

Use this when a browser game includes third-person / camera-relative movement, drag-look, or shooter controls.

- Lock the control contract before writing code: with the camera behind the player, W moves away from camera, S toward camera, A screen-left, D screen-right.
- Drag/look contract: dragging right yaws right; dragging left yaws left; dragging upward must not invert pitch unless the brief explicitly asks for inverted look.
- Use one canonical camera frame everywhere: ground-projected camera-forward for movement, matching right-vector math, and one shared yaw/pitch state for camera + aim.
- Do NOT fake TPS drag-look by mapping absolute cursor screen position directly into yaw/pitch targets like `mouse.x -> targetYaw`; use drag delta / pointer-lock delta accumulation instead.
- Prefer explicit formulas over vague intent:
  - `forward = cameraForwardOnGround.normalize()`
  - If your movement forward basis is `forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw))`, use `right = new THREE.Vector3(forward.z, 0, -forward.x).normalize()`.
  - If your movement forward basis is `forward = new THREE.Vector3(-Math.sin(yaw), 0, -Math.cos(yaw))`, use `right = new THREE.Vector3(-forward.z, 0, forward.x).normalize()`.
  - `A => move.sub(right)`, `D => move.add(right)`
- Projectile contract for shooter/TPS work:
  - Spawn shots from a muzzle / barrel / weapon-tip anchor or a deliberate forward aim origin tied to the weapon.
  - Do NOT spawn bullets from `camera.position` or the avatar root unless there is an explicit muzzle offset proving visual alignment.
  - Keep a visible tracer / projectile core plus muzzle flash and impact feedback so QA can read the bullet path.
- Builder must self-check a 6-point matrix before finishing: `W/S`, `A/D`, drag-right, drag-left, drag-up, drag-down.
- Reviewer/tester must reject if any movement axis feels mirrored, if yaw direction is flipped, or if upward drag behaves like inverted look without being requested.
- Analyst should pass downstream nodes concrete controller references and sign conventions, not only art/style references.
- If an existing project already works, patch the broken input/camera math in place; do not rewrite the whole game shell just to fix controls.
