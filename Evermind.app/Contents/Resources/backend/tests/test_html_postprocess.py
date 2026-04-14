import unittest
import tempfile
from pathlib import Path

from html_postprocess import (
    materialize_local_runtime_assets,
    postprocess_html,
    postprocess_javascript,
    postprocess_stylesheet,
)


class HtmlPostprocessTests(unittest.TestCase):
    def test_postprocess_html_injects_game_runtime_perf_shim_for_three_game(self):
        source = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TPS</title>
  <style>body{margin:0}canvas{display:block}</style>
</head>
<body>
  <canvas id="game"></canvas>
  <script src="./_evermind_runtime/three/three.min.js"></script>
  <script>
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    const clock = new THREE.Clock();
  </script>
</body>
</html>"""

        result = postprocess_html(source, task_type="game")

        self.assertIn('data-evermind-runtime-shim="game-perf"', result)
        self.assertIn("__EVERMIND_PAGE_HIDDEN__", result)
        self.assertIn("__EVERMIND_SAFE_RENDER", result)
        self.assertIn("renderer.render(scene, camera);", result)
        self.assertNotIn("window.__EVERMIND_SAFE_RENDER(renderer, scene, camera);", result)
        self.assertIn("Math.min(requested || 1, 1.5)", result)
        self.assertIn("Math.min(delta, 0.05)", result)

    def test_postprocess_javascript_guards_optional_shared_hooks(self):
        source = (
            "const nav = document.getElementById('nav');\n"
            "const overlay = document.querySelector('.page-transition-overlay');\n"
            "nav.classList.add('scrolled');\n"
            "overlay.classList.add('active');\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("if (nav) { nav.classList.add('scrolled'); }", result)
        self.assertIn("if (overlay) { overlay.classList.add('active'); }", result)

    def test_postprocess_javascript_normalizes_page_transition_selector_variants(self):
        source = (
            "const overlay = document.querySelector('.page-transition');\n"
            "overlay.classList.add('active');\n"
        )

        result = postprocess_javascript(source)

        self.assertIn(
            "document.querySelector('.page-transition, .page-transition-overlay')",
            result,
        )
        self.assertIn("if (overlay) { overlay.classList.add('active'); }", result)

    def test_postprocess_javascript_rewrites_commonjs_export_footer_for_browser(self):
        source = (
            "class WeaponManager {\n"
            "  equip() { return 'rifle'; }\n"
            "}\n"
            "module.exports = { WeaponManager };\n"
        )

        result = postprocess_javascript(source)

        self.assertNotIn("module.exports", result)
        self.assertIn("window.WeaponManager = WeaponManager;", result)

    def test_postprocess_javascript_upgrades_invalid_three_mesh_basic_material_configs(self):
        source = (
            "const mat = new THREE.MeshBasicMaterial({\n"
            "  color: 0x7ce8ff,\n"
            "  emissive: 0x14506a,\n"
            "  emissiveIntensity: 1.8,\n"
            "  roughness: 0.16,\n"
            "});\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("new THREE.MeshStandardMaterial({", result)
        self.assertNotIn("new THREE.MeshBasicMaterial({", result)
        self.assertIn("emissive: 0x14506a", result)

    def test_postprocess_javascript_keeps_plain_three_mesh_basic_material_configs(self):
        source = "const mat = new THREE.MeshBasicMaterial({ color: 0xffff00 });\n"

        result = postprocess_javascript(source)

        self.assertIn("new THREE.MeshBasicMaterial({ color: 0xffff00 });", result)
        self.assertNotIn("MeshStandardMaterial", result)

    def test_postprocess_javascript_guards_common_three_render_calls(self):
        source = (
            "function animate() {\n"
            "  renderer.render(scene, camera);\n"
            "  requestAnimationFrame(animate);\n"
            "}\n"
            "class TacticalGame {\n"
            "  draw() { this.renderer.render(this.scene, this.camera); }\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("window.__EVERMIND_SAFE_RENDER(renderer, scene, camera);", result)
        self.assertIn("window.__EVERMIND_SAFE_RENDER(this.renderer, this.scene, this.camera);", result)
        self.assertNotIn("renderer.render(scene, camera);", result)

    def test_postprocess_javascript_normalizes_common_tps_orbit_drag_signs(self):
        source = (
            "const cameraState = { yaw: 0, pitch: 0, distance: 8, sensitivity: 0.004 };\n"
            "function updateCamera() {\n"
            "  const offset = new THREE.Vector3(0, 0, cameraState.distance);\n"
            "  offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), cameraState.pitch);\n"
            "  offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), cameraState.yaw);\n"
            "}\n"
            "document.addEventListener('mousemove', (e) => {\n"
            "  const deltaX = e.clientX - lastMouseX;\n"
            "  const deltaY = e.clientY - lastMouseY;\n"
            "  cameraState.yaw -= deltaX * cameraState.sensitivity;\n"
            "  cameraState.pitch -= deltaY * cameraState.sensitivity;\n"
            "});\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("cameraState.yaw += deltaX * cameraState.sensitivity;", result)
        self.assertIn("cameraState.pitch -= deltaY * cameraState.sensitivity;", result)
        self.assertNotIn("cameraState.yaw -= deltaX", result)
        self.assertNotIn("cameraState.pitch += deltaY", result)

    def test_postprocess_javascript_normalizes_class_based_tps_target_pitch_updates(self):
        source = (
            "class TPSCameraRig {\n"
            "  processInput(deltaX, deltaY) {\n"
            "    this.targetYaw -= deltaX * this.config.sensitivityX;\n"
            "    this.targetPitch -= deltaY * this.config.sensitivityY;\n"
            "  }\n"
            "  update(right) {\n"
            "    const pitchQuat = new THREE.Quaternion().setFromAxisAngle(right, this.pitch);\n"
            "    this.camera.lookAt(this.currentLookAt);\n"
            "  }\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("this.targetPitch -= deltaY * this.config.sensitivityY;", result)
        self.assertNotIn("this.targetPitch += deltaY", result)

    def test_postprocess_javascript_normalizes_manual_tps_trig_orbit_drag_signs(self):
        source = (
            "let cameraYaw = 0;\n"
            "let cameraPitch = 0.25;\n"
            "function updateCamera() {\n"
            "  const offset = new THREE.Vector3(\n"
            "    Math.sin(cameraYaw) * Math.cos(cameraPitch) * 8,\n"
            "    Math.sin(cameraPitch) * 8 + 2.5,\n"
            "    Math.cos(cameraYaw) * Math.cos(cameraPitch) * 8\n"
            "  );\n"
            "  camera.position.copy(player.position.clone().add(offset));\n"
            "  camera.lookAt(player.position);\n"
            "}\n"
            "document.addEventListener('mousemove', (e) => {\n"
            "  const deltaX = e.clientX - lastMouseX;\n"
            "  const deltaY = e.clientY - lastMouseY;\n"
            "  cameraYaw -= deltaX * 0.004;\n"
            "  cameraPitch -= deltaY * 0.004;\n"
            "});\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("cameraYaw += deltaX * 0.004;", result)
        self.assertIn("cameraPitch -= deltaY * 0.004;", result)
        self.assertNotIn("cameraYaw -= deltaX", result)
        self.assertNotIn("cameraPitch += deltaY", result)

    def test_postprocess_javascript_normalizes_tps_trig_orbit_alias_chain_signs(self):
        source = (
            "const gameState = { cameraYaw: 0, cameraPitch: 0.3 };\n"
            "function updateCamera() {\n"
            "  const targetYaw = gameState.cameraYaw;\n"
            "  const targetPitch = THREE.MathUtils.clamp(gameState.cameraPitch, -0.5, 1.2);\n"
            "  const distance = 8;\n"
            "  const offset = new THREE.Vector3(\n"
            "    Math.sin(targetYaw) * Math.cos(targetPitch) * distance,\n"
            "    Math.sin(targetPitch) * distance + 2.5,\n"
            "    Math.cos(targetYaw) * Math.cos(targetPitch) * distance\n"
            "  );\n"
            "  camera.position.copy(player.position.clone().add(offset));\n"
            "  camera.lookAt(player.position);\n"
            "}\n"
            "function onMouseMove(e) {\n"
            "  gameState.cameraYaw -= e.movementX * 0.004;\n"
            "  gameState.cameraPitch -= e.movementY * 0.004;\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("gameState.cameraYaw += e.movementX * 0.004;", result)
        self.assertIn("gameState.cameraPitch -= e.movementY * 0.004;", result)
        self.assertNotIn("gameState.cameraYaw -= e.movementX", result)
        self.assertNotIn("gameState.cameraPitch += e.movementY", result)

    def test_postprocess_javascript_normalizes_tps_trig_orbit_cached_cospitch_alias_signs(self):
        source = (
            "const gameState = { cameraYaw: 0, cameraPitch: 0.3, cameraDistance: 8 };\n"
            "function updateCamera() {\n"
            "  const targetYaw = gameState.cameraYaw;\n"
            "  const targetPitch = THREE.MathUtils.clamp(gameState.cameraPitch, -0.5, 1.2);\n"
            "  const targetDistance = gameState.cameraDistance;\n"
            "  const cosPitch = Math.cos(targetPitch);\n"
            "  const offset = new THREE.Vector3(\n"
            "    Math.sin(targetYaw) * cosPitch * targetDistance,\n"
            "    Math.sin(targetPitch) * targetDistance + 2.5,\n"
            "    Math.cos(targetYaw) * cosPitch * targetDistance\n"
            "  );\n"
            "  camera.position.copy(player.position.clone().add(offset));\n"
            "  camera.lookAt(player.position);\n"
            "}\n"
            "function onMouseMove(e) {\n"
            "  gameState.cameraYaw -= e.movementX * CONFIG.camera.sensitivity;\n"
            "  gameState.cameraPitch -= e.movementY * CONFIG.camera.sensitivity;\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("gameState.cameraYaw += e.movementX * CONFIG.camera.sensitivity;", result)
        self.assertIn("gameState.cameraPitch -= e.movementY * CONFIG.camera.sensitivity;", result)
        self.assertNotIn("gameState.cameraYaw -= e.movementX", result)
        self.assertNotIn("gameState.cameraPitch += e.movementY", result)

    def test_postprocess_javascript_normalizes_apply_euler_tps_orbit_drag_signs(self):
        source = (
            "let cameraYaw = 0;\n"
            "let cameraPitch = 0.3;\n"
            "const cameraDistance = 8;\n"
            "function updateCamera() {\n"
            "  const offset = new THREE.Vector3(0, 0, cameraDistance);\n"
            "  offset.applyEuler(new THREE.Euler(cameraPitch, cameraYaw, 0));\n"
            "  camera.position.copy(player.position.clone().add(offset));\n"
            "  camera.lookAt(player.position);\n"
            "}\n"
            "document.addEventListener('mousemove', (e) => {\n"
            "  const deltaX = e.clientX - lastMouseX;\n"
            "  const deltaY = e.clientY - lastMouseY;\n"
            "  cameraYaw -= deltaX * cameraSensitivity;\n"
            "  cameraPitch -= deltaY * cameraSensitivity;\n"
            "});\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("cameraYaw += deltaX * cameraSensitivity;", result)
        self.assertIn("cameraPitch -= deltaY * cameraSensitivity;", result)
        self.assertNotIn("cameraYaw -= deltaX", result)
        self.assertNotIn("cameraPitch += deltaY", result)

    def test_postprocess_javascript_normalizes_member_alias_tps_drag_signs(self):
        source = (
            "class CameraController {\n"
            "  updateCamera() {\n"
            "    this._offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), this._currentPitch);\n"
            "    this._offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), this._currentYaw);\n"
            "  }\n"
            "  onMouseMove(deltaX, deltaY) {\n"
            "    this.yaw -= deltaX * this.sensitivity;\n"
            "    this.pitch -= deltaY * this.sensitivity;\n"
            "  }\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("this.yaw += deltaX * this.sensitivity;", result)
        self.assertIn("this.pitch -= deltaY * this.sensitivity;", result)
        self.assertNotIn("this.yaw -= deltaX", result)
        self.assertNotIn("this.pitch += deltaY", result)

    def test_postprocess_javascript_moves_rotated_offset_tps_camera_behind_player(self):
        source = (
            "let yaw = 0;\n"
            "let pitch = 0.25;\n"
            "const cameraDistance = 10;\n"
            "function updateCamera() {\n"
            "  const offset = new THREE.Vector3(0, 0, cameraDistance);\n"
            "  offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), pitch);\n"
            "  offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), yaw);\n"
            "  camera.position.copy(player.position.clone()).add(offset);\n"
            "  camera.lookAt(player.position);\n"
            "}\n"
            "function updateMovement() {\n"
            "  const forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)).normalize();\n"
            "  const right = new THREE.Vector3(forward.z, 0, -forward.x).normalize();\n"
            "  if (keys['KeyW']) move.add(forward);\n"
            "  if (keys['KeyS']) move.sub(forward);\n"
            "  if (keys['KeyA']) move.sub(right);\n"
            "  if (keys['KeyD']) move.add(right);\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("new THREE.Vector3(0, 0, -cameraDistance);", result)
        self.assertNotIn("new THREE.Vector3(0, 0, cameraDistance);", result)

    def test_postprocess_javascript_normalizes_tps_strafe_math_and_mapping(self):
        source = (
            "function updateMovement() {\n"
            "  const right = new THREE.Vector3(-forward.z, 0, forward.x).normalize();\n"
            "  if (keys['KeyA'] || keys['ArrowLeft']) { move.add(right); }\n"
            "  if (keys['KeyD'] || keys['ArrowRight']) { move.sub(right); }\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("new THREE.Vector3(forward.z, 0, -forward.x).normalize();", result)
        self.assertIn("if (keys['KeyA'] || keys['ArrowLeft']) { move.sub(right); }", result)
        self.assertIn("if (keys['KeyD'] || keys['ArrowRight']) { move.add(right); }", result)
        self.assertNotIn("new THREE.Vector3(-forward.z, 0, forward.x).normalize();", result)
        self.assertNotIn("if (keys['KeyA'] || keys['ArrowLeft']) { move.add(right); }", result)
        self.assertNotIn("if (keys['KeyD'] || keys['ArrowRight']) { move.sub(right); }", result)

    def test_postprocess_javascript_keeps_inverted_forward_strafe_basis_consistent(self):
        source = (
            "function updateMovement() {\n"
            "  const forward = new THREE.Vector3(-Math.sin(yaw), 0, -Math.cos(yaw)).normalize();\n"
            "  const right = new THREE.Vector3(forward.z, 0, -forward.x).normalize();\n"
            "  if (keys['KeyA']) move.sub(right);\n"
            "  if (keys['KeyD']) move.add(right);\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("const right = new THREE.Vector3(-forward.z, 0, forward.x).normalize();", result)
        self.assertIn("if (keys['KeyA']) move.sub(right);", result)
        self.assertIn("if (keys['KeyD']) move.add(right);", result)
        self.assertNotIn("const right = new THREE.Vector3(forward.z, 0, -forward.x).normalize();", result)

    def test_postprocess_javascript_normalizes_negative_offset_tps_orbit_drag_signs(self):
        source = (
            "const cameraState = { yaw: 0, pitch: 0.2, distance: 8, sensitivity: 0.004 };\n"
            "function updateCamera() {\n"
            "  const offset = new THREE.Vector3(0, 0, -cameraState.distance);\n"
            "  offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), cameraState.pitch);\n"
            "  offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), cameraState.yaw);\n"
            "}\n"
            "document.addEventListener('mousemove', (e) => {\n"
            "  const deltaX = e.clientX - lastMouseX;\n"
            "  const deltaY = e.clientY - lastMouseY;\n"
            "  cameraState.yaw += deltaX * cameraState.sensitivity;\n"
            "  cameraState.pitch += deltaY * cameraState.sensitivity;\n"
            "});\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("cameraState.yaw -= deltaX * cameraState.sensitivity;", result)
        self.assertIn("cameraState.pitch -= deltaY * cameraState.sensitivity;", result)
        self.assertNotIn("cameraState.yaw += deltaX", result)
        self.assertNotIn("cameraState.pitch += deltaY", result)

    def test_postprocess_javascript_normalizes_clamp_style_tps_drag_assignments(self):
        source = (
            "let cameraYaw = 0;\n"
            "let cameraPitch = 0.25;\n"
            "function updateCamera() {\n"
            "  const offset = new THREE.Vector3(\n"
            "    Math.sin(cameraYaw) * Math.cos(cameraPitch) * 8,\n"
            "    Math.sin(cameraPitch) * 8 + 2.5,\n"
            "    Math.cos(cameraYaw) * Math.cos(cameraPitch) * 8\n"
            "  );\n"
            "  camera.position.copy(player.position.clone().add(offset));\n"
            "}\n"
            "function onMouseMove(e) {\n"
            "  cameraYaw = cameraYaw - e.movementX * 0.004;\n"
            "  cameraPitch = Math.max(-0.5, Math.min(1.2, cameraPitch - e.movementY * 0.004));\n"
            "}\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("cameraYaw = cameraYaw + e.movementX * 0.004;", result)
        self.assertIn(
            "cameraPitch = Math.max(-0.5, Math.min(1.2, cameraPitch - e.movementY * 0.004));",
            result,
        )
        self.assertNotIn("cameraYaw = cameraYaw - e.movementX", result)
        self.assertNotIn("cameraPitch + e.movementY", result)

    def test_postprocess_javascript_normalizes_minus_minus_tps_orbit_yaw_sign(self):
        source = (
            "let yaw = 0;\n"
            "let pitch = 0.2;\n"
            "function updateCamera() {\n"
            "  camera.position.x = player.position.x - Math.sin(yaw) * Math.cos(pitch) * cameraDist;\n"
            "  camera.position.y = player.position.y + Math.sin(pitch) * cameraDist + 2;\n"
            "  camera.position.z = player.position.z - Math.cos(yaw) * Math.cos(pitch) * cameraDist;\n"
            "}\n"
            "document.addEventListener('mousemove', (e) => {\n"
            "  const deltaX = e.clientX - lastMouseX;\n"
            "  const deltaY = e.clientY - lastMouseY;\n"
            "  yaw += deltaX * sensitivity;\n"
            "  pitch += deltaY * sensitivity;\n"
            "});\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("yaw -= deltaX * sensitivity;", result)
        self.assertIn("pitch -= deltaY * sensitivity;", result)
        self.assertNotIn("yaw += deltaX * sensitivity;", result)

    def test_postprocess_javascript_repairs_invalid_object_literal_member_mutations(self):
        source = (
            "const bullet = {\n"
            "  position: gameState.player.position.clone(),\n"
            "  position.y: 1.2,\n"
            "  rotation: weapon.rotation.clone(),\n"
            "  rotation.z: 0.25,\n"
            "  speed: 18,\n"
            "};\n"
        )

        result = postprocess_javascript(source)

        self.assertIn("position: (() => {", result)
        self.assertIn("const __evermind_position_1 = gameState.player.position.clone();", result)
        self.assertIn("__evermind_position_1.y = 1.2;", result)
        self.assertIn("rotation: (() => {", result)
        self.assertIn("const __evermind_rotation_2 = weapon.rotation.clone();", result)
        self.assertIn("__evermind_rotation_2.z = 0.25;", result)
        self.assertNotIn("position.y:", result)
        self.assertNotIn("rotation.z:", result)

    def test_postprocess_html_adds_reverse_page_transition_alias(self):
        source = (
            "<!DOCTYPE html><html><head><title>X</title></head>"
            "<body><div class=\"page-transition-overlay\"></div></body></html>"
        )

        result = postprocess_html(source)

        self.assertIn('class="page-transition-overlay page-transition"', result)

    def test_postprocess_html_strips_remote_font_links(self):
        source = (
            "<!DOCTYPE html><html><head>"
            "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
            "<link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap\" rel=\"stylesheet\">"
            "</head><body><main>ok</main></body></html>"
        )

        result = postprocess_html(source)

        self.assertNotIn("fonts.googleapis.com", result)
        self.assertIn("<main>ok</main>", result)

    def test_postprocess_html_repairs_head_body_order_when_body_starts_inside_head(self):
        source = (
            "<!DOCTYPE html><html><head><title>X</title><style>body{margin:0}"
            "<body><main>ok</main></html>"
        )

        result = postprocess_html(source)

        self.assertIn("</head>", result.lower())
        self.assertIn("</style>", result.lower())
        self.assertTrue(result.lower().index("</head>") < result.lower().index("<body"))
        self.assertIn("</body>", result.lower())

    def test_postprocess_stylesheet_strips_remote_font_imports(self):
        source = (
            "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap');\n"
            "body { font-family: 'Inter', sans-serif; }\n"
        )

        result = postprocess_stylesheet(source)

        self.assertNotIn("fonts.googleapis.com", result)
        self.assertIn("font-family", result)

    def test_postprocess_game_html_adds_low_height_preview_safety_overrides(self):
        source = (
            "<!DOCTYPE html><html><head><style>"
            "html,body{margin:0;height:100%;overflow:hidden}"
            ".app{height:100%}.frame{height:min(92vh,880px)}"
            ".overlay{position:absolute}.modal{padding:24px}.feature-row{display:grid}.result-grid{display:grid}"
            "</style></head><body><div class='app'><div class='frame'><canvas></canvas><div class='overlay'><div class='modal'></div></div></div></div></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("@media (max-height: 820px)", result)
        self.assertIn(".frame, .layout-frame, .game-frame", result)
        self.assertIn(".feature-row, .result-grid", result)

    def test_postprocess_game_html_adds_preview_safety_for_variant_shell_names(self):
        source = (
            "<!DOCTYPE html><html><head><style>"
            "html,body{margin:0;height:100%;overflow:hidden}"
            ".layout-root{min-height:100dvh}.shell-frame{height:100vh}"
            ".menu-overlay{position:absolute;inset:0}.menu-modal{padding:24px}"
            "</style></head><body><div class='layout-root'><div class='shell-frame'>"
            "<canvas></canvas><div class='menu-overlay'><div class='menu-modal'>menu</div></div>"
            "</div></div></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("@media (max-height: 820px)", result)
        self.assertIn(".shell-frame", result)
        self.assertIn(".menu-overlay", result)
        self.assertIn(".menu-modal", result)

    def test_postprocess_game_html_adds_compact_hud_safety_for_preview_windows(self):
        source = (
            "<!DOCTYPE html><html><head><style>"
            "#app{position:relative;height:100vh}.hud{display:flex}.hud-bottom{display:flex}"
            ".hud-card{padding:12px}.weapon-panel{min-width:420px}.weapon-name{font-size:26px}"
            ".ammo-big{font-size:30px}.mini-tag{white-space:nowrap}"
            "</style></head><body><div id='app'><canvas></canvas><div class='hud'>"
            "<div class='hud-bottom'><div class='weapon-panel'></div><div class='hud-card'></div></div>"
            "</div></div></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("@media (max-height: 860px)", result)
        self.assertIn(".hud-bottom > .weapon-panel", result)
        self.assertIn(".mini-tag { padding: 6px 9px !important;", result)

    def test_postprocess_game_html_adds_overlay_safe_area_rules_for_menu_screens(self):
        source = (
            "<!DOCTYPE html><html><head><style>"
            "#app{position:relative;height:100vh}.menu-screen,.briefing-screen,.pause-screen,.game-over-screen{position:absolute;inset:0}"
            "</style></head><body><div id='app'><canvas id='gameCanvas'></canvas>"
            "<div class='menu-screen'><div class='menu-card'>menu</div></div>"
            "</div></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn(".menu-screen, .briefing-screen, .pause-screen, .game-over-screen", result)
        self.assertIn("max-height: calc(100dvh - 28px)", result)
        self.assertIn("canvas, #gameCanvas { touch-action: none !important; }", result)

    def test_postprocess_game_html_hides_hidden_screens_from_qa_visibility_checks(self):
        source = (
            "<!DOCTYPE html><html><head><style>"
            ".screen{display:flex;position:fixed;inset:0}"
            ".screen.hidden{opacity:0;pointer-events:none}"
            "</style></head><body>"
            "<div id='startScreen' class='screen hidden'>开始游戏</div>"
            "<canvas id='gameCanvas'></canvas>"
            "</body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("display: none !important;", result)
        self.assertIn("visibility: hidden !important;", result)

    def test_postprocess_game_html_localizes_remote_engine_runtime_urls(self):
        source = (
            "<!DOCTYPE html><html><head>"
            "<script src='https://cdn.jsdelivr.net/npm/phaser@3.80.1/dist/phaser.min.js'></script>"
            "</head><body><script>const game = new Phaser.Game({type: Phaser.AUTO});</script></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("./_evermind_runtime/phaser/phaser.min.js", result)
        self.assertNotIn("cdn.jsdelivr.net/npm/phaser", result)

    def test_postprocess_game_html_injects_local_three_runtime_when_global_usage_exists(self):
        source = (
            "<!DOCTYPE html><html><head><title>Voxel</title></head>"
            "<body><script>const scene = new THREE.Scene();</script></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("./_evermind_runtime/three/three.min.js", result)
        self.assertLess(
            result.index("./_evermind_runtime/three/three.min.js"),
            result.index("const scene = new THREE.Scene();"),
        )

    def test_postprocess_game_html_rewrites_invalid_inline_three_basic_material_configs(self):
        source = (
            "<!DOCTYPE html><html><head><title>Voxel</title></head><body><script>"
            "const mesh = new THREE.Mesh(new THREE.BoxGeometry(1,1,1), "
            "new THREE.MeshBasicMaterial({ color: 0x08a6ff, emissive: 0x02263e, roughness: 0.42 }));"
            "</script></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("MeshStandardMaterial({ color: 0x08a6ff, emissive: 0x02263e, roughness: 0.42 })", result)
        self.assertNotIn("MeshBasicMaterial({ color: 0x08a6ff, emissive: 0x02263e, roughness: 0.42 })", result)

    def test_postprocess_game_html_dedupes_three_classic_when_local_module_import_exists(self):
        source = (
            "<!DOCTYPE html><html><head>"
            "<script src='./_evermind_runtime/three/three.min.js'></script>"
            "</head><body><script type='module'>"
            "import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';"
            "const scene = new THREE.Scene();"
            "</script></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("./_evermind_runtime/three/three.module.js", result)
        self.assertNotIn("cdn.jsdelivr.net/npm/three", result)
        self.assertNotIn("./_evermind_runtime/three/three.min.js", result)

    def test_postprocess_game_html_stabilizes_weapon_ammo_state_before_initial_hud_render(self):
        source = (
            "<!DOCTYPE html><html><head><script>"
            "const weapons = [{ name:'Tempest AR', magSize:36, reserve:180 }];"
            "const game = { currentWeaponIndex:0, ammo:[] };"
            "function getWeapon(){"
            "  return weapons[game.currentWeaponIndex];"
            "}"
            "function updateHUD(){"
            "  const weapon = getWeapon();"
            "  const ammo = game.ammo[game.currentWeaponIndex];"
            "  console.log(weapon.name, ammo.mag);"
            "}"
            "updateHUD();"
            "</script></head><body></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("function _evermindSafeWeaponIndex", result)
        self.assertIn("return _evermindSafeWeapon();", result)
        self.assertIn("const ammo = _evermindSafeAmmo();", result)
        self.assertNotIn("const ammo = game.ammo[game.currentWeaponIndex];", result)

    def test_postprocess_game_html_injects_capsule_geometry_compat_shim(self):
        source = (
            "<!DOCTYPE html><html><head>"
            "<script src='./_evermind_runtime/three/three.min.js'></script>"
            "</head><body><script>"
            "const geometry = new THREE.CapsuleGeometry(0.35, 1.2, 4, 8);"
            "</script></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn('data-evermind-runtime-shim="three-capsule"', result)
        self.assertIn("typeof THREE.CapsuleGeometry !== 'function'", result)
        self.assertIn("new THREE.CapsuleGeometry(0.35, 1.2, 4, 8)", result)

    def test_postprocess_game_html_wraps_pointer_lock_requests_with_safe_helper(self):
        source = (
            "<!DOCTYPE html><html><head></head><body><script>"
            "canvas.addEventListener('mousedown', () => { canvas.requestPointerLock(); });"
            "</script></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn('data-evermind-runtime-shim="pointer-lock"', result)
        self.assertIn("_evermindSafeRequestPointerLock(canvas);", result)
        self.assertNotIn("canvas.requestPointerLock();", result)

    def test_postprocess_game_html_keeps_pointer_lock_shim_non_recursive(self):
        source = (
            "<!DOCTYPE html><html><head></head><body><script>"
            "canvas.addEventListener('click', () => { canvas.requestPointerLock({ unadjustedMovement: true }); });"
            "</script></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn('data-evermind-runtime-shim="pointer-lock"', result)
        self.assertIn("target.requestPointerLock(options);", result)
        self.assertIn("_evermindSafeRequestPointerLock(canvas, { unadjustedMovement: true });", result)
        self.assertNotIn(": _evermindSafeRequestPointerLock(target, options);", result)

    def test_postprocess_game_html_normalizes_method_style_pointer_lock_helper_calls(self):
        source = (
            "<!DOCTYPE html><html><head></head><body><script>"
            "this._evermindSafeRequestPointerLock(canvas);"
            "</script></body></html>"
        )

        result = postprocess_html(source, task_type="game")

        self.assertIn("_evermindSafeRequestPointerLock(this.canvas);", result)
        self.assertNotIn("this._evermindSafeRequestPointerLock(", result)

    def test_materialize_local_runtime_assets_copies_vendor_files_next_to_output_html(self):
        with tempfile.TemporaryDirectory() as td:
            html_path = Path(td) / "index.html"
            html_path.write_text(
                "<!DOCTYPE html><html><body><script src='./_evermind_runtime/howler/howler.min.js'></script></body></html>",
                encoding="utf-8",
            )

            materialized = materialize_local_runtime_assets(html_path, task_type="game")
            runtime_path = Path(td) / "_evermind_runtime" / "howler" / "howler.min.js"
            self.assertTrue(runtime_path.exists())
            self.assertIn(runtime_path, materialized)
            self.assertGreater(runtime_path.stat().st_size, 1000)


if __name__ == '__main__':
    unittest.main()
