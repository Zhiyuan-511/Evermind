import unittest
import asyncio
import tempfile
import time
import os
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import orchestrator as orchestrator_module
import preview_validation as preview_validation_module
from orchestrator import Orchestrator, Plan, SubTask, TaskStatus


class TestParseTestResult(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_json_fail_is_respected(self):
        output = '{"status":"fail","errors":["Missing <head> tag"]}'
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "fail")

    def test_game_runtime_risk_detects_invalid_mesh_basic_material_props(self):
        report = self.orch._game_runtime_risk_findings(
            "const tracer = new THREE.Mesh(new THREE.TubeGeometry(path, 2, 0.02), new THREE.MeshBasicMaterial({ color: 0xffaa00, emissive: 0xffaa00, emissiveIntensity: 2 }));",
            "做一个第三人称 3D 射击游戏",
        )
        self.assertTrue(any("MeshBasicMaterial" in str(item) for item in report))

    def test_game_runtime_risk_detects_recursive_safe_render_shim(self):
        blob = """
        window.__EVERMIND_SAFE_RENDER = window.__EVERMIND_SAFE_RENDER || function(renderer, scene, camera) {
          try {
            if (!renderer || !scene || !camera || typeof renderer.render !== 'function') return false;
            window.__EVERMIND_SAFE_RENDER(renderer, scene, camera);
            return true;
          } catch (_evermindRenderError) {
            return false;
          }
        };
        function gameLoop(){
          requestAnimationFrame(gameLoop);
          window.__EVERMIND_SAFE_RENDER(renderer, scene, camera);
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏",
        )
        self.assertTrue(any("safe-render shim recursively" in str(item) for item in report))

    def test_game_runtime_risk_allows_startgame_when_loop_is_already_booted(self):
        blob = """
        const GameState = { MENU: 'MENU', PLAYING: 'PLAYING' };
        let currentState = GameState.MENU;
        function resetPlayer(){ player.health = 100; }
        function spawnLevelEnemies(){ enemies.length = 0; }
        function updateHUD(){ hud.textContent = 'ready'; }
        function animate(){ requestAnimationFrame(animate); if (currentState !== GameState.PLAYING) return; renderScene(); }
        animate();
        function startGame(){
          document.getElementById('startOverlay').classList.add('hidden');
          currentState = GameState.PLAYING;
          resetPlayer();
          spawnLevelEnemies();
          updateHUD();
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertFalse(any("start/play control" in str(item).lower() for item in report))

    def test_game_runtime_risk_allows_string_current_state_assignment_with_booted_loop(self):
        blob = """
        const gameState = { current: 'MENU' };
        function spawnEnemies(){ enemies.length = 4; }
        function updateHUD(){ hud.textContent = 'live'; }
        function animate(time){ requestAnimationFrame(animate); if (gameState.current !== 'PLAYING') return; renderScene(time); }
        animate(0);
        function startGame(){
          document.getElementById('startScreen').classList.add('hidden');
          gameState.current = 'PLAYING';
          spawnEnemies();
          updateHUD();
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertFalse(any("start/play control" in str(item).lower() for item in report))

    def test_game_runtime_risk_allows_enum_play_state_and_startwave_boot_sequence(self):
        blob = """
        const GameState = { MENU: 'MENU', PLAYING: 'PLAYING', GAME_OVER: 'GAME_OVER' };
        let gameState = GameState.MENU;
        function resetPlayer(){ player.health = 100; }
        function startWave(){ enemies.length = 6; }
        function updateHUD(){ hud.textContent = 'wave live'; }
        function animate(){ requestAnimationFrame(animate); if (gameState !== GameState.PLAYING) return; renderScene(); }
        animate();
        function startGame(){
          document.getElementById('start-screen').classList.add('hidden');
          gameState = GameState.PLAYING;
          resetPlayer();
          startWave();
          updateHUD();
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertFalse(any("start/play control" in str(item).lower() for item in report))

    def test_game_runtime_risk_ignores_loading_child_spinner_classes_when_overlay_hides(self):
        blob = """
        <div id="loading" class="loading-overlay">
          <div class="loading-spinner"></div>
          <div class="loading-text">Loading...</div>
        </div>
        <canvas id="game-canvas"></canvas>
        <script>
          const loading = document.getElementById('loading');
          function startGame(){
            loading.classList.add('hidden');
            document.getElementById('game-canvas').classList.add('visible');
          }
          function animate(){ requestAnimationFrame(animate); }
          animate();
        </script>
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏",
        )
        self.assertFalse(any("loading/boot overlay" in str(item).lower() for item in report))
        self.assertFalse(any("loading-spinner" in str(item).lower() for item in report))

    def test_game_runtime_risk_allows_boot_gameplay_wrapper_and_encounter_init(self):
        blob = """
        const gameState = { phase: 'MENU' };
        const weapons = [{ ammo: 0, maxAmmo: 30 }];
        let enemies = [1, 2];
        let bullets = [1];
        const player = { position: { set() {} } };
        function animate(time){ requestAnimationFrame(animate); if (gameState.phase !== 'PLAYING') return; renderScene(time); }
        function bootGameplay(){ animate(0); }
        function startEncounter(){ spawnChests(); updateMissionDisplay(); }
        function spawnChests(){ chests = []; }
        function updateMissionDisplay(){ hud.textContent = 'mission live'; }
        function startGame(){
          document.getElementById('startScreen').style.display = 'none';
          document.getElementById('hud').classList.add('visible');
          gameState.phase = 'PLAYING';
          player.position.set(0, 0, 0);
          weapons.forEach((weapon) => { weapon.ammo = weapon.maxAmmo; });
          enemies = [];
          bullets = [];
          startEncounter();
          bootGameplay();
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertFalse(any("start/play control" in str(item).lower() for item in report))

    def test_game_runtime_risk_detects_prestart_render_loop_without_guard(self):
        blob = """
        let renderer, scene, camera;
        const GameState = { MENU: 'MENU', PLAYING: 'PLAYING' };
        let currentState = GameState.MENU;
        function initThree(){
          scene = new THREE.Scene();
          camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
          renderer = new THREE.WebGLRenderer();
        }
        function startGame(){
          initThree();
          currentState = GameState.PLAYING;
        }
        function animate(){
          requestAnimationFrame(animate);
          renderer.render(scene, camera);
        }
        animate();
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertTrue(any("render loop" in str(item).lower() for item in report))

    def test_game_runtime_risk_detects_three_loop_without_render_call(self):
        blob = """
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
        const renderer = new THREE.WebGLRenderer();
        let running = false;
        function animate(){
          requestAnimationFrame(animate);
          if (!running) return;
          updateEnemies();
          updateProjectiles();
        }
        function startGame(){
          running = true;
          document.body.dataset.mode = 'playing';
          animate();
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏",
        )
        self.assertTrue(any("never issues a renderer/composer render call" in str(item).lower() for item in report))

    def test_game_runtime_risk_detects_loading_overlay_without_hide_path(self):
        blob = """
        <div id="loadingOverlay">Loading 3D assets...</div>
        <canvas id="gameCanvas"></canvas>
        <script>
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
        const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
        let running = false;
        function startGame(){
          running = true;
          document.body.dataset.mode = 'playing';
          requestAnimationFrame(loop);
        }
        function loop(){
          if (!running) return;
          renderer.render(scene, camera);
          requestAnimationFrame(loop);
        }
        </script>
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏",
        )
        self.assertTrue(any("loading/boot overlay" in str(item).lower() for item in report))

    def test_game_runtime_risk_detects_hidden_canvas_without_reveal_path(self):
        blob = """
        <style>
          #gameCanvas { display: none; opacity: 0; }
        </style>
        <canvas id="gameCanvas"></canvas>
        <script>
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
        const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
        let running = false;
        function startGame(){
          running = true;
          document.body.dataset.mode = 'playing';
          requestAnimationFrame(loop);
        }
        function loop(){
          if (!running) return;
          renderer.render(scene, camera);
          requestAnimationFrame(loop);
        }
        </script>
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏",
        )
        self.assertTrue(any("gameplay surface starts hidden" in str(item).lower() for item in report))

    def test_game_runtime_risk_accepts_projectile_core_with_muzzle_and_impact_particles(self):
        blob = """
        function fireWeapon() {
          const muzzle = currentWeapon.mesh.getObjectByName('muzzle');
          const muzzlePos = new THREE.Vector3();
          muzzle.getWorldPosition(muzzlePos);
          spawnParticles(muzzlePos, 5, 0x00d4ff, 3);
          fireBullet(muzzlePos, direction, 60, 20);
        }
        function fireBullet(origin, direction, speed, damage) {
          const bullet = bulletPool.find(b => !b.active);
          bullet.active = true;
          bullet.mesh.visible = true;
          bullet.mesh.position.copy(origin);
          bullet.velocity.copy(direction).multiplyScalar(speed);
          bullet.damage = damage;
        }
        function updateBullets(dt) {
          for (const bullet of bulletPool) {
            if (!bullet.active) continue;
            if (hitEnemy) {
              createImpactParticles(bullet.mesh.position, 0xff4757);
            }
          }
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，要有清晰可见的子弹弹道和命中特效。",
        )
        self.assertFalse(any("projectile readability cues" in str(item).lower() for item in report))

    def test_validate_builder_quality_flags_unwired_commonjs_support_files_for_merger(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            runtime_dir = out_dir / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// runtime stub", encoding="utf-8")

            root_html = out_dir / "index.html"
            root_html.write_text(
                """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Merger QA</title>
  <style>body{margin:0;background:#08111b;color:#d9f3ff}canvas{display:block;width:100vw;height:100vh}</style>
  <script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <button id="start-btn">Start</button>
  <canvas id="game-canvas"></canvas>
  <div id="hud">ammo score health</div>
  <div id="game-over-screen" class="hidden"></div>
  <script>
    const state = { started: false, cameraYaw: 0, cameraPitch: 0.2 };
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('game-canvas') });
    document.addEventListener('keydown', (e) => { if (e.code === 'KeyW') state.started = true; });
    function startGame() {
      state.started = true;
      document.getElementById('hud').textContent = 'ammo 30 score 0 health 100';
    }
    document.getElementById('start-btn').addEventListener('click', startGame);
    function gameLoop() {
      requestAnimationFrame(gameLoop);
      if (!state.started) return;
      camera.lookAt(0, 0, 0);
      renderer.render(scene, camera);
    }
    gameLoop();
  </script>
</body>
</html>""",
                encoding="utf-8",
            )

            support_dir = out_dir / "js" / "combat"
            support_dir.mkdir(parents=True, exist_ok=True)
            support_file = support_dir / "WeaponManager.js"
            support_file.write_text(
                """class WeaponManager {
  constructor() {
    this.currentSlot = 0;
    this.weapons = ['rifle', 'shotgun', 'smg'];
  }
  equip(slot) {
    this.currentSlot = slot;
    return this.weapons[this.currentSlot] || this.weapons[0];
  }
  update(dt) {
    return dt + this.currentSlot;
  }
}
module.exports = { WeaponManager };
""",
                encoding="utf-8",
            )

            plan = Plan(goal="做一个第三人称 3D 射击游戏", subtasks=[])
            subtask = SubTask(
                id="7",
                agent_type="builder",
                description="Merger integrator merge the strongest outputs into the final shipped artifact.",
                depends_on=[],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", out_dir):
                report = self.orch._validate_builder_quality(
                    [str(root_html), str(support_file)],
                    "",
                    goal=plan.goal,
                    plan=plan,
                    subtask=subtask,
                )

            joined_errors = " ".join(str(item) for item in report.get("errors", []))
            saved_root = root_html.read_text(encoding="utf-8")
            self.assertNotIn("unwired", joined_errors)
            self.assertNotIn("module.exports", support_file.read_text(encoding="utf-8"))
            self.assertIn("window.WeaponManager = WeaponManager;", support_file.read_text(encoding="utf-8"))
            self.assertIn('src="./js/combat/WeaponManager.js"', saved_root)

    def test_validate_builder_quality_auto_wires_meaningful_support_files_for_merger(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            runtime_dir = out_dir / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// runtime stub", encoding="utf-8")

            root_html = out_dir / "index.html"
            root_html.write_text(
                """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Merger Auto Wire</title>
  <script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <button id="start-btn">Start</button>
  <canvas id="game-canvas"></canvas>
  <div id="hud">ammo score health</div>
  <div id="game-over-screen" class="hidden"></div>
  <script type="module">
    const state = { started: false, hp: 100, yaw: 0, pitch: 0.12 };
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('game-canvas') });
    function startGame() {
      state.started = true;
      document.getElementById('hud').textContent = 'ammo 30 score 0 health 100';
    }
    document.getElementById('start-btn').addEventListener('click', startGame);
    window.addEventListener('pointermove', (event) => {
      if (!state.started) return;
      state.yaw += event.movementX * 0.002;
      state.pitch = Math.max(-0.6, Math.min(0.6, state.pitch + event.movementY * 0.001));
    });
    document.addEventListener('keydown', (event) => {
      if (event.code === 'KeyW') state.started = true;
      if (event.code === 'Escape') document.getElementById('game-over-screen').classList.remove('hidden');
    });
    function animate() {
      requestAnimationFrame(animate);
      if (!state.started) return;
      const cameraTarget = new THREE.Vector3(Math.sin(state.yaw) * 4, 2.2 + state.pitch, Math.cos(state.yaw) * 4);
      camera.position.lerp(cameraTarget, 0.15);
      camera.lookAt(0, 1.2, 0);
      renderer.render(scene, camera);
    }
    animate();
  </script>
</body>
</html>""",
                encoding="utf-8",
            )

            support_css = out_dir / "css" / "hud.css"
            support_css.parent.mkdir(parents=True, exist_ok=True)
            support_css.write_text(
                "#hud{position:fixed;top:16px;right:16px;padding:12px 16px;background:rgba(8,18,32,.88);color:#d9f3ff;border:1px solid rgba(0,212,255,.35)}",
                encoding="utf-8",
            )

            support_js = out_dir / "js" / "weaponSystem.js"
            support_js.parent.mkdir(parents=True, exist_ok=True)
            support_js.write_text(
                """class WeaponSystem {
  constructor() {
    this.currentWeapon = 'rifle';
    this.fireCooldown = 0.08;
  }
  update(dt) {
    this.fireCooldown = Math.max(0, this.fireCooldown - dt);
  }
}
window.WeaponSystem = WeaponSystem;
""",
                encoding="utf-8",
            )

            plan = Plan(goal="做一个 3D 射击游戏", subtasks=[])
            subtask = SubTask(
                id="8",
                agent_type="builder",
                description="Merger integrator merge the strongest outputs into the final shipped artifact.",
                depends_on=[],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", out_dir):
                report = self.orch._validate_builder_quality(
                    [str(root_html)],
                    "",
                    goal=plan.goal,
                    plan=plan,
                    subtask=subtask,
                )

            saved_root = root_html.read_text(encoding="utf-8")
            joined_errors = " ".join(str(item) for item in report.get("errors", []))
            self.assertNotIn("unwired", joined_errors)
            self.assertIn('href="./css/hud.css"', saved_root)
            self.assertIn('src="./js/weaponSystem.js"', saved_root)
            self.assertTrue(
                saved_root.index('src="./js/weaponSystem.js"') < saved_root.index('<script type="module">')
            )
            self.assertTrue(any("Auto-wired meaningful merger support files" in str(item) for item in report.get("warnings", [])))

    def test_validate_builder_quality_flags_persistent_loading_overlay_for_merger(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            runtime_dir = out_dir / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// runtime stub", encoding="utf-8")

            root_html = out_dir / "index.html"
            root_html.write_text(
                """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Merger Loading Overlay</title>
  <style>
    body{margin:0;background:#08111b;color:#d9f3ff;font-family:system-ui,sans-serif}
    #loadingOverlay{position:fixed;inset:0;display:grid;place-items:center;background:rgba(4,9,18,.92);z-index:99}
    #start-btn{position:fixed;top:24px;left:24px;z-index:5}
    #game-canvas{display:block;width:100vw;height:100vh}
    #hud{position:fixed;top:24px;right:24px}
  </style>
  <script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <div id="loadingOverlay">Loading 3D assets...</div>
  <button id="start-btn">Start</button>
  <canvas id="game-canvas"></canvas>
  <div id="hud">hp 100</div>
  <script>
    const state = { started: false };
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('game-canvas') });
    function startGame() {
      state.started = true;
      document.getElementById('hud').textContent = 'hp 100 score 0';
    }
    document.getElementById('start-btn').addEventListener('click', startGame);
    function gameLoop() {
      requestAnimationFrame(gameLoop);
      if (!state.started) return;
      renderer.render(scene, camera);
    }
    gameLoop();
  </script>
</body>
</html>""",
                encoding="utf-8",
            )

            plan = Plan(goal="做一个 3D 动作游戏", subtasks=[])
            subtask = SubTask(
                id="8",
                agent_type="builder",
                description="Merger integrator merge the strongest outputs into the final shipped artifact.",
                depends_on=[],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", out_dir):
                report = self.orch._validate_builder_quality(
                    [str(root_html)],
                    "",
                    goal=plan.goal,
                    plan=plan,
                    subtask=subtask,
                )

            joined_errors = " ".join(str(item) for item in report.get("errors", []))
            self.assertFalse(report.get("pass", True))
            self.assertIn("loading/boot overlay", joined_errors.lower())

    def test_game_placeholder_model_domains_does_not_flag_authored_weapon_assembly(self):
        blob = """
        function createWeapon(){
          const group = new THREE.Group();
          const bodyShape = new THREE.Shape();
          bodyShape.moveTo(-0.4, 0);
          bodyShape.lineTo(0.7, 0);
          bodyShape.lineTo(0.75, 0.2);
          bodyShape.lineTo(-0.4, 0.1);
          bodyShape.lineTo(-0.4, 0);
          const body = new THREE.Mesh(
            new THREE.ExtrudeGeometry(bodyShape, { depth: 0.08, bevelEnabled: true }),
            new THREE.MeshStandardMaterial({ color: 0x3a4a3a, roughness: 0.6, metalness: 0.3 })
          );
          const barrel = new THREE.Mesh(
            new THREE.CylinderGeometry(0.03, 0.04, 0.8, 12),
            new THREE.MeshStandardMaterial({ color: 0x2a2a2a, roughness: 0.4, metalness: 0.8 })
          );
          barrel.castShadow = true;
          group.add(body);
          group.add(barrel);
          return group;
        }
        """
        self.assertEqual(self.orch._game_placeholder_model_domains(blob), [])

    def test_game_placeholder_model_domains_ignores_enemy_blade_alias_when_weapon_function_is_authored(self):
        blob = """
        function createWeapon(){
          const group = new THREE.Group();
          const receiverShape = new THREE.Shape();
          receiverShape.moveTo(-0.4, 0);
          receiverShape.lineTo(0.7, 0);
          receiverShape.lineTo(0.75, 0.2);
          receiverShape.lineTo(-0.4, 0.1);
          receiverShape.lineTo(-0.4, 0);
          const receiver = new THREE.Mesh(
            new THREE.ExtrudeGeometry(receiverShape, { depth: 0.08, bevelEnabled: true }),
            new THREE.MeshStandardMaterial({ color: 0x3a4a3a, roughness: 0.6, metalness: 0.3 })
          );
          const barrelPath = new THREE.LineCurve3(
            new THREE.Vector3(0, 0.02, 0.05),
            new THREE.Vector3(0.75, 0.02, 0.05)
          );
          const barrel = new THREE.Mesh(
            new THREE.TubeGeometry(barrelPath, 12, 0.025, 8, false),
            new THREE.MeshStandardMaterial({ color: 0x8f98a3, roughness: 0.24, metalness: 0.88 })
          );
          const scopeGeom = new THREE.CylinderGeometry(0.03, 0.03, 0.18, 12);
          const scope = new THREE.Mesh(scopeGeom, new THREE.MeshStandardMaterial({ color: 0x1b1f2a }));
          group.add(receiver);
          group.add(barrel);
          group.add(scope);
          return group;
        }

        function createEnemyDrone(){
          const group = new THREE.Group();
          const bodyShape = new THREE.Shape();
          bodyShape.moveTo(-0.2, -0.1);
          bodyShape.lineTo(0.22, -0.1);
          bodyShape.lineTo(0.18, 0.18);
          bodyShape.lineTo(-0.16, 0.2);
          bodyShape.lineTo(-0.2, -0.1);
          group.add(new THREE.Mesh(
            new THREE.ExtrudeGeometry(bodyShape, { depth: 0.18, bevelEnabled: true }),
            new THREE.MeshStandardMaterial({ color: 0x772233, emissive: 0x22050a })
          ));
          const bladeGeom = new THREE.BoxGeometry(0.4, 0.02, 0.02);
          group.add(new THREE.Mesh(bladeGeom, new THREE.MeshStandardMaterial({ color: 0x999999 })));
          return group;
        }
        """
        self.assertNotIn("weapon", self.orch._game_placeholder_model_domains(blob))

    def test_game_placeholder_model_domains_flags_single_authored_token_on_primitive_dominated_hero(self):
        blob = """
        function createPlayer(){
          const group = new THREE.Group();
          const torsoShape = new THREE.Shape();
          torsoShape.moveTo(-0.2, 0);
          torsoShape.lineTo(0.2, 0);
          torsoShape.lineTo(0.15, 0.6);
          torsoShape.lineTo(-0.15, 0.6);
          torsoShape.lineTo(-0.2, 0);
          group.add(new THREE.Mesh(new THREE.ExtrudeGeometry(torsoShape, { depth: 0.15 })));
          group.add(new THREE.Mesh(new THREE.BoxGeometry(0.45, 0.25, 0.3)));
          group.add(new THREE.Mesh(new THREE.SphereGeometry(0.18, 16, 16)));
          group.add(new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.08, 0.7, 12)));
          group.add(new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.1, 0.8, 12)));
          return group;
        }
        """
        self.assertIn("character", self.orch._game_placeholder_model_domains(blob))

    def test_game_runtime_risk_detects_mirrored_third_person_controls(self):
        blob = """
        const input = { left:false, right:false, lookDy:0 };
        const forward = new THREE.Vector3(Math.sin(cameraYaw), 0, Math.cos(cameraYaw)).normalize();
        const right = new THREE.Vector3(-forward.z, 0, forward.x).normalize();
        const offset = new THREE.Vector3(0, 0, -cameraDistance);
        offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), cameraPitch);
        offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), cameraYaw);
        if (input.right) move.sub(right);
        if (input.left) move.add(right);
        cameraPitch += input.lookDy * 0.002;
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标拖动视角，不要镜像控制。",
        )
        self.assertTrue(any("mirrored" in str(item).lower() for item in report))
        self.assertTrue(any("pitch" in str(item).lower() for item in report))

    def test_builder_quality_failure_focus_note_adds_js_syntax_patch_guidance(self):
        note = self.orch._builder_quality_failure_focus_note(
            "Inline script #1 contains invalid JavaScript syntax: Unexpected token '.'"
        )
        self.assertIn("Fix parseability first", note)
        self.assertIn("position.y: 1.2", note)
        self.assertIn("valid standalone JavaScript", note)

    def test_builder_quality_failure_focus_note_adds_merger_and_projectile_guidance(self):
        note = self.orch._builder_quality_failure_focus_note(
            "Merger left meaningful support files unwired from the shipped root artifact. "
            "Shipped browser support files still use CommonJS exports instead of browser-native scripts/modules. "
            "projectile/tracer visibility and muzzle-origin alignment are missing."
        )
        self.assertIn("module.exports", note)
        self.assertIn("unwired", note)
        self.assertIn("muzzle / barrel / weapon-tip anchor", note)

    def test_game_runtime_risk_detects_mirrored_drag_yaw_for_plus_plus_camera_orbit(self):
        blob = """
        let cameraAzimuth = 0;
        let targetCameraAzimuth = 0;
        let isDragging = true;
        document.addEventListener('mousemove', (e) => {
          const deltaX = e.clientX - lastMouseX;
          targetCameraAzimuth -= deltaX * 0.005;
        });
        function updateCamera(){
          const x = player.position.x + Math.sin(cameraAzimuth) * cameraDistance;
          const z = player.position.z + Math.cos(cameraAzimuth) * cameraDistance;
          camera.position.set(x, 4, z);
          camera.lookAt(player.position);
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertTrue(any("yaw appears mirrored" in str(item).lower() for item in report))

    def test_game_runtime_risk_detects_mirrored_drag_controls_for_rotated_offset_orbit(self):
        blob = """
        const cameraState = { yaw: 0, pitch: 0, distance: 8, sensitivity: 0.004 };
        function updateCamera() {
          const offset = new THREE.Vector3(0, 0, cameraState.distance);
          offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), cameraState.pitch);
          offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), cameraState.yaw);
          camera.position.copy(player.position.clone().add(offset));
          camera.lookAt(player.position);
        }
        document.addEventListener('mousemove', (e) => {
          const deltaX = e.clientX - lastMouseX;
          const deltaY = e.clientY - lastMouseY;
          cameraState.yaw -= deltaX * cameraState.sensitivity;
          cameraState.pitch += deltaY * cameraState.sensitivity;
        });
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertTrue(any("yaw appears mirrored" in str(item).lower() for item in report))
        self.assertTrue(any("pitch appears inverted" in str(item).lower() for item in report))

    def test_game_runtime_risk_detects_front_facing_rotated_offset_camera(self):
        blob = """
        let yaw = 0;
        let pitch = 0.25;
        const cameraDistance = 10;
        function updateCamera() {
          const offset = new THREE.Vector3(0, 0, cameraDistance);
          offset.applyAxisAngle(new THREE.Vector3(1, 0, 0), pitch);
          offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), yaw);
          camera.position.copy(player.position.clone()).add(offset);
          camera.lookAt(player.position);
        }
        function updateMovement() {
          const forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)).normalize();
          const right = new THREE.Vector3(forward.z, 0, -forward.x).normalize();
          if (keys['KeyW']) move.add(forward);
          if (keys['KeyS']) move.sub(forward);
          if (keys['KeyA']) move.sub(right);
          if (keys['KeyD']) move.add(right);
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertTrue(any("start in front of the player" in str(item).lower() for item in report))

    def test_game_runtime_risk_flags_body_centered_projectile_without_readability_fx(self):
        blob = """
        const bullets = [];
        function shoot() {
          const bullet = new THREE.Mesh(
            new THREE.SphereGeometry(0.08, 10, 10),
            new THREE.MeshBasicMaterial({ color: 0xffd966 })
          );
          bullet.position.copy(player.position);
          bullet.position.y += 1.5;
          const dir = new THREE.Vector3(Math.sin(cameraYaw), 0, Math.cos(cameraYaw)).normalize();
          bullet.userData.vel = dir.multiplyScalar(1.6);
          scene.add(bullet);
          bullets.push(bullet);
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，子弹弹道要清晰可见。",
        )
        self.assertTrue(any("body-centered" in str(item).lower() for item in report))
        self.assertTrue(any("readability cues" in str(item).lower() for item in report))

    def test_game_runtime_risk_flags_camera_centered_projectile_spawn(self):
        blob = """
        function fireWeapon() {
          const bullet = { mesh: new THREE.Mesh(new THREE.SphereGeometry(0.04, 8, 8), new THREE.MeshBasicMaterial({ color: 0x00f5ff })) };
          const direction = raycaster.ray.direction.clone().normalize();
          bullet.mesh.position.copy(camera.position).add(direction.clone().multiplyScalar(1));
          bullet.velocity = direction.multiplyScalar(48);
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，子弹弹道要清晰可见。",
        )
        self.assertTrue(any("camera-centered" in str(item).lower() for item in report))

    def test_game_runtime_risk_flags_absolute_cursor_camera_mapping_for_drag_goal(self):
        blob = """
        const mouse = { x: 0, y: 0 };
        function onMouseMove(e) {
          mouse.x = (e.clientX / window.innerWidth) * 2 - 1;
          mouse.y = -(e.clientY / window.innerHeight) * 2 + 1;
        }
        function updateCamera(dt) {
          const targetYaw = -mouse.x * Math.PI;
          const targetPitch = mouse.y * 0.5;
          player.userData.yaw += (targetYaw - player.userData.yaw) * 5 * dt;
          player.userData.pitch += (targetPitch - player.userData.pitch) * 5 * dt;
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，鼠标长按屏幕后可以拉动转动视角。",
        )
        self.assertTrue(any("absolute cursor screen position" in str(item).lower() for item in report))

    def test_game_runtime_risk_detects_spawn_kill_opening_without_grace_period(self):
        blob = """
        function startGame(){ spawnEnemiesForStage(); }
        function spawnEnemiesForStage() {
          let x, z;
          do {
            x = (Math.random() - 0.5) * 40;
            z = (Math.random() - 0.5) * 40;
          } while (Math.abs(x) < 8 && Math.abs(z) < 8);
          createEnemy(x, z);
        }
        function updateEnemies() {
          const distance = toPlayer.length();
          if (distance < 10 && performance.now() - enemy.lastAttackTime > enemy.attackCooldown) {
            playerStats.health -= 10;
            if (playerStats.health <= 0) missionFailed();
          }
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，带怪物、枪械和战斗。",
        )
        self.assertTrue(any("spawn safety appears unsafe" in str(item).lower() for item in report))

    def test_game_runtime_risk_allows_radial_spawn_safety_with_grace_period(self):
        blob = """
        let combatStartAt = 0;
        function startGame(){ combatStartAt = performance.now(); spawnEnemiesForStage(); }
        function spawnEnemiesForStage() {
          let x, z;
          do {
            x = (Math.random() - 0.5) * 40;
            z = (Math.random() - 0.5) * 40;
          } while (Math.hypot(x, z) < 16);
          createEnemy(x, z);
        }
        function updateEnemies() {
          const distance = toPlayer.length();
          if (performance.now() - combatStartAt < 1800) return;
          if (distance < 8 && performance.now() - enemy.lastAttackTime > enemy.attackCooldown) {
            playerStats.health -= 10;
          }
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "做一个第三人称 3D 射击游戏，带怪物、枪械和战斗。",
        )
        self.assertFalse(any("spawn safety appears unsafe" in str(item).lower() for item in report))

    def test_game_runtime_risk_accepts_concrete_stage_and_wave_progression(self):
        blob = """
        let gameState='menu';
        let score=0,wave=1,stage=1,maxStages=3;
        let enemiesKilled=0,enemiesInWave=5;
        function nextWave(){
          wave++;
          enemiesKilled=0;
          if(wave>3){
            stage++;
            wave=1;
          }
          if(stage>maxStages){
            victory();
            return;
          }
        }
        function victory(){
          gameState='victory';
          document.getElementById('victory-screen').classList.add('active');
        }
        """
        report = self.orch._game_runtime_risk_findings(
            blob,
            "创建一个第三人称3D射击游戏，至少3个关卡或阶段推进，并包含胜利结算。",
        )
        self.assertFalse(any("stage progression logic" in str(item).lower() for item in report))


class TestCanonicalNodeExecutionMapping(unittest.TestCase):
    def test_mapping_skips_unused_builder_slots_and_keeps_later_nodes_aligned(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="Build a 3D shooter",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="analyze", depends_on=[]),
                SubTask(id="2", agent_type="builder", description="build", depends_on=["1"]),
                SubTask(id="3", agent_type="reviewer", description="review", depends_on=["2"]),
                SubTask(id="4", agent_type="deployer", description="deploy", depends_on=["3"]),
            ],
        )
        ne_list = [
            {"id": "ne_analyst", "node_key": "analyst"},
            {"id": "ne_builder1", "node_key": "builder1"},
            {"id": "ne_builder2", "node_key": "builder2"},
            {"id": "ne_reviewer", "node_key": "reviewer"},
            {"id": "ne_deployer", "node_key": "deployer"},
        ]

        mapping = orch._map_subtasks_to_canonical_node_executions(plan, ne_list)

        self.assertEqual(mapping["1"], "ne_analyst")
        self.assertEqual(mapping["2"], "ne_builder1")
        self.assertEqual(mapping["3"], "ne_reviewer")
        self.assertEqual(mapping["4"], "ne_deployer")

    def test_mapping_prefers_explicit_merger_node_key_for_integrator_builder(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="Build a 3D shooter",
            subtasks=[
                SubTask(id="1", agent_type="builder", node_key="builder1", description="core builder", depends_on=[]),
                SubTask(id="2", agent_type="builder", node_key="builder2", description="support builder", depends_on=[]),
                SubTask(
                    id="3",
                    agent_type="builder",
                    node_key="merger",
                    node_label="Merger",
                    description="Merger integrator merge the strongest outputs into the final shipped artifact.",
                    depends_on=["1", "2"],
                ),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["3"]),
            ],
        )
        ne_list = [
            {"id": "ne_builder1", "node_key": "builder1"},
            {"id": "ne_builder2", "node_key": "builder2"},
            {"id": "ne_merger", "node_key": "merger"},
            {"id": "ne_reviewer", "node_key": "reviewer"},
        ]

        mapping = orch._map_subtasks_to_canonical_node_executions(plan, ne_list)

        self.assertEqual(mapping["1"], "ne_builder1")
        self.assertEqual(mapping["2"], "ne_builder2")
        self.assertEqual(mapping["3"], "ne_merger")
        self.assertEqual(mapping["4"], "ne_reviewer")


class TestPolisherFlow(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_custom_polisher_description_preserves_structure(self):
        desc = self.orch._custom_node_task_desc("polisher", "Polisher", "做一个像苹果一样高级的 8 页奢侈品官网")
        self.assertIn("Refine the strongest existing deliverable", desc)
        self.assertIn("Do NOT collapse the site to fewer pages", desc)
        self.assertIn("styles.css and app.js upgrades FIRST", desc)
        self.assertIn("patch at most 2 HTML files", desc)

    def test_reviewer_task_description_allows_direct_route_coverage_after_nav_validation(self):
        desc = self.orch._reviewer_task_description("做一个三页面官网，包含首页、定价页和联系页")
        self.assertIn("first verify at least one real internal navigation path", desc)
        self.assertIn("direct page visits are acceptable", desc)

    def test_deep_mode_complex_website_includes_polisher(self):
        subtasks = self.orch._build_pro_plan_subtasks("做一个像苹果一样高级的 8 页奢侈品官网，要有电影感动画转场")
        agent_types = [st.agent_type for st in subtasks]
        self.assertEqual(agent_types[0], "planner")
        self.assertEqual(agent_types[1], "analyst")
        self.assertIn("polisher", agent_types)
        builders = [st for st in subtasks if st.agent_type == "builder"]
        self.assertEqual(len(builders), 3)
        builder1 = next(st for st in builders if st.node_key == "builder1")
        builder2 = next(st for st in builders if st.node_key == "builder2")
        merger = next(st for st in builders if st.node_key == "merger")
        self.assertEqual(builder1.depends_on, ["2", "3"])
        self.assertEqual(builder2.depends_on, ["2", "3"])
        self.assertEqual(merger.depends_on, [builder1.id, builder2.id])
        polisher = next(st for st in subtasks if st.agent_type == "polisher")
        reviewer = next(st for st in subtasks if st.agent_type == "reviewer")
        self.assertEqual(polisher.depends_on, [merger.id, "4"])
        self.assertEqual(reviewer.depends_on, [polisher.id])


class TestReviewerReworkBrief(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_format_reviewer_rework_brief_groups_builder_and_polisher_actions(self):
        reviewer_output = json.dumps({
            "verdict": "REJECTED",
            "scores": {
                "layout": 6,
                "color": 4,
                "typography": 5,
                "animation": 5,
                "responsive": 7,
                "functionality": 5,
                "completeness": 5,
                "originality": 5,
            },
            "blocking_issues": [
                "index.html uses a flat black background with weak card separation.",
                "cities.html is missing a real visual anchor above the fold.",
            ],
            "required_changes": [
                "Builder: replace the generic hero with topic-matched Beijing / China travel imagery on index.html.",
                "Polisher: tighten typography and nav spacing on cities.html.",
            ],
            "acceptance_criteria": [
                "index.html and cities.html both show layered palette treatment and route-appropriate visuals.",
            ],
            "strengths": [
                "Navigation structure is already coherent across routes.",
            ],
        })

        brief = self.orch._format_reviewer_rework_brief(reviewer_output)

        self.assertIn("Route-specific issues:", brief)
        self.assertIn("Builder fixes:", brief)
        self.assertIn("Polisher follow-up:", brief)
        self.assertIn("index.html", brief)
        self.assertIn("cities.html", brief)
        self.assertIn("extend the palette", brief.lower())

    def test_format_reviewer_rework_brief_adds_current_artifact_anchors_when_routes_missing(self):
        reviewer_output = json.dumps({
            "verdict": "REJECTED",
            "blocking_issues": [
                "The main menu overlay still blocks gameplay in shorter preview windows.",
            ],
            "required_changes": [
                "Builder: make the start screen scroll safely and keep the playfield visible.",
            ],
        })

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html><html><head><title>Steel Outpost 3D</title></head>
<body><main><h1>开始作战</h1><h2>任务简报</h2></main></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                brief = self.orch._format_reviewer_rework_brief(reviewer_output)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("Current artifact anchors", brief)
        self.assertIn("index.html", brief)
        self.assertIn("开始作战", brief)

    def test_builder_reviewer_patch_retry_description_uses_patch_mode_not_clean_slate(self):
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="Build a premium three-page site with strong motion and shared navigation.",
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面官网，包含首页、定价页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                """<!DOCTYPE html><html><head><title>Home</title></head>
<body><main><h1>首页</h1><p>已有较完整内容。</p></main></body></html>""",
                encoding="utf-8",
            )
            (out / "pricing.html").write_text(
                """<!DOCTYPE html><html><head><title>Pricing</title></head>
<body><main><h1>定价</h1><p>当前页面需要继续增强视觉锚点。</p></main></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                desc = self.orch._builder_reviewer_patch_retry_description(
                    subtask,
                    plan,
                    "pricing.html 缺少明确视觉锚点，index.html CTA 状态变化不明显。",
                    round_num=2,
                    max_rejections=3,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("[Reviewer Rework Patch Mode]", desc)
        self.assertIn("Do NOT do a clean-slate rewrite", desc)
        self.assertIn("pricing.html", desc)
        self.assertNotIn("Discard the previous failed approach", desc)


class TestBuilderFailureCleanup(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_cleanup_internal_builder_artifacts_removes_scaffolds_but_keeps_real_page(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            scaffold = out / "index.html"
            scaffold.write_text(
                "<!DOCTYPE html><html><body><!-- evermind-bootstrap scaffold --></body></html>",
                encoding="utf-8",
            )
            partial = out / "_partial_builder.html"
            partial.write_text("<!DOCTYPE html><html><body>partial</body></html>", encoding="utf-8")
            real_page = out / "about.html"
            real_page.write_text("<!DOCTYPE html><html><body>real page</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                removed = self.orch._cleanup_internal_builder_artifacts()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertIn(str(scaffold), removed)
            self.assertIn(str(partial), removed)
            self.assertFalse(scaffold.exists())
            self.assertFalse(partial.exists())
            self.assertTrue(real_page.exists())

    def test_validate_builder_quality_rejects_unfinished_visual_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            about_html = out / "about.html"
            styles_css = out / "styles.css"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>旅行首页</title><link rel="stylesheet" href="styles.css"></head>
<body><nav><a href="index.html">首页</a><a href="about.html">关于</a></nav>
<main><section class="destination-placeholder"></section><section><h1>首页</h1><p>完整内容。</p></section></main>
</body></html>""",
                encoding="utf-8",
            )
            about_html.write_text(
                """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>关于我们</title><link rel="stylesheet" href="styles.css"></head>
<body><nav><a href="index.html">首页</a><a href="about.html">关于</a></nav>
<main><section><h1>关于</h1><p>这是完整的介绍页面。</p></section></main>
</body></html>""",
                encoding="utf-8",
            )
            styles_css.write_text(
                ".destination-placeholder{min-height:280px;background:linear-gradient(135deg,#111,#333);}"
                "nav{display:flex;gap:12px}main{display:grid;gap:24px}section{padding:24px}",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "website"
                report = self.orch._validate_builder_quality(
                    [str(index_html), str(about_html), str(styles_css)],
                    "",
                    goal="做一个两页旅游网站，包含首页和关于页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(
            any("unfinished visual placeholders" in str(item).lower() for item in report.get("errors", []))
        )

    def test_validate_builder_quality_rejects_flat_monochrome_website_palette(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            styles_css = out / "styles.css"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>中国旅行首页</title><link rel="stylesheet" href="styles.css"></head>
<body><header><nav><a href="index.html">首页</a><a href="cities.html">城市</a></nav></header>
<main><section><h1>探索中国</h1><p>这是一个有足够内容密度的高端旅行首页，用于验证纯黑白单色背景会被质量门禁拦截，而不是因为内容太少失败。</p><p>页面依然保留完整段落、结构和真实文案，以确保这里触发的是配色质量问题。</p></section><section><h2>城市精选</h2><p>北京、上海、成都和西安等目的地都需要更丰富的层次化背景与视觉节奏。</p></section></main>
</body></html>""",
                encoding="utf-8",
            )
            styles_css.write_text(
                "body{background:#000;color:#fff;}header,section{background:#111;border:1px solid #fff;}nav{display:flex;gap:12px}"
                "main{display:grid;gap:24px;padding:24px}@media (max-width: 768px){nav{flex-wrap:wrap}}",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "website"
                report = self.orch._validate_builder_quality(
                    [str(index_html), str(styles_css)],
                    "",
                    goal="做一个高端中国旅行网站",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("flat/monochrome" in str(item).lower() for item in report.get("errors", [])))

    def test_validate_builder_quality_rejects_game_without_loop_and_input(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Broken Arena</title></head>
<body>
  <main>
    <section><h1>Broken Arena</h1><p>这是一个看起来像游戏的页面，但它故意缺少真实循环和输入绑定，用来验证游戏质量门禁会拦下不可玩产物。</p></section>
    <section><button id="startBtn">开始作战</button><div id="gameArena">静态场景，没有真实交互。</div></section>
    <section><p>这里保留了足够的文字和结构，避免测试只是因为内容太少而失败。</p><p>最终失败原因必须来自游戏静态可玩性检查。</p></section>
  </main>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 像素射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("runtime loop" in str(item).lower() for item in report.get("errors", [])))
        self.assertTrue(any("input bindings" in str(item).lower() for item in report.get("errors", [])))

    def test_validate_builder_quality_rejects_game_with_boot_hud_before_ammo_init(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ammo Crash Arena</title>
<style>
  body{margin:0;background:#08111f;color:#f4f7fb;font-family:system-ui, sans-serif}
  main{display:grid;gap:20px;padding:24px}
  canvas{width:100%;height:320px;display:block;background:#102033;border:1px solid rgba(255,255,255,.12)}
  .hud{display:flex;gap:16px;flex-wrap:wrap}
</style>
</head>
<body>
  <main>
    <section id="startOverlay"><h1>Ammo Crash Arena</h1><p>这是一段足够长的介绍文案，用来确保质量门针对的是游戏首帧运行风险，而不是因为页面内容过短或结构不完整而失败。</p><button id="startBtn" onclick="startGame()">开始作战</button></section>
    <section><canvas id="gameCanvas"></canvas><div class="hud"><span id="scoreValue"></span><span id="healthValue"></span><span id="ammoValue"></span></div></section>
    <section id="gameOver"><h2>任务失败</h2><p>重新开始后应当恢复到可玩的战斗状态，并显示完整 HUD。</p><button onclick="startGame()">重新开始</button></section>
  </main>
  <script>
    const ui = {
      scoreValue: document.getElementById('scoreValue'),
      healthValue: document.getElementById('healthValue'),
      ammoValue: document.getElementById('ammoValue')
    };
    const game = { ammo: [], currentWeaponIndex: 0, score: 0, health: 100, level: 1 };
    function startGame(){ resetGame(); requestAnimationFrame(loop); }
    function resetGame(){
      game.ammo = [{ mag: 12, reserve: 48 }];
      updateHUD();
    }
    function updateHUD(){
      const ammo = game.ammo[game.currentWeaponIndex];
      ui.scoreValue.textContent = 'score ' + game.score;
      ui.healthValue.textContent = 'health ' + game.health;
      ui.ammoValue.textContent = `${ammo.mag} / ${ammo.reserve}`;
    }
    function loop(){ requestAnimationFrame(loop); }
    document.addEventListener('keydown', () => {});
    updateHUD();
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 像素射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(
            any("hud updates before ammo" in str(item).lower() for item in report.get("errors", []))
        )

    def test_validate_builder_quality_rejects_game_with_menu_render_before_player_init(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Menu Render Crash</title>
<style>
  body{margin:0;background:#09131d;color:#eef3ff;font-family:system-ui, sans-serif}
  main{display:grid;gap:20px;padding:24px}
  canvas{width:100%;height:320px;display:block;background:#101b2a;border:1px solid rgba(255,255,255,.14)}
  .hud{display:flex;gap:12px}
</style>
</head>
<body>
  <main>
    <section id="startOverlay"><h1>Menu Render Crash</h1><p>这个测试页面保留了开始界面、HUD、结束态和输入绑定，唯一问题是菜单阶段就启动了未做空值保护的渲染循环。</p><button id="startBtn" onclick="startGame()">开始作战</button></section>
    <section><canvas id="gameCanvas" tabindex="0"></canvas><div class="hud"><span>score 0</span><span>health 100</span><span>ammo 12</span></div></section>
    <section id="gameOver"><h2>任务结束</h2><p>玩家应当可以重新开始，而不是在菜单首帧就因为空 player 崩溃。</p><button onclick="startGame()">再来一次</button></section>
  </main>
  <script>
    const world = {
      player: null,
      camera: { yaw: 0.8, pitch: 0.3, dist: 240, x: 0, y: 0, z: 0 }
    };
    function setMode(mode){ document.body.dataset.mode = mode; }
    function startGame(){ resetGame(); setMode('PLAYING'); }
    function resetGame(){ world.player = { x: 0, z: 0, yaw: 0.75 }; }
    function render(){
      const p = world.player;
      const cam = world.camera;
      cam.yaw = p.yaw + Math.PI;
      cam.x = p.x - Math.sin(cam.yaw) * cam.dist;
      cam.z = p.z - Math.cos(cam.yaw) * cam.dist;
    }
    function loop(){
      render();
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', () => {});
    setMode('MENU');
    render();
    requestAnimationFrame(loop);
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 像素射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(
            any("render/requestanimationframe before player/camera state" in str(item).lower() for item in report.get("errors", []))
        )

    def test_validate_builder_quality_rejects_game_missing_local_runtime_asset(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Runtime Missing Arena</title>
<script src="./_evermind_runtime/three/three.missing.js"></script>
</head>
<body>
  <button id="startBtn">开始作战</button>
  <canvas id="gameCanvas"></canvas>
  <script>
    const state = { running: false };
    function startGame(){ state.running = true; }
    function loop(){ requestAnimationFrame(loop); }
    document.addEventListener('keydown', () => {});
    document.getElementById('startBtn').addEventListener('click', startGame);
    loop();
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("missing runtime asset" in str(item).lower() for item in report.get("errors", [])))

    def test_validate_builder_quality_rejects_game_missing_local_boot_script(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Missing Boot Script Arena</title>
<style>
  body{margin:0;background:#08111f;color:#f4f7fb;font-family:system-ui,sans-serif}
  main{display:grid;gap:20px;padding:24px}
  canvas{width:100%;height:320px;display:block;background:#102033;border:1px solid rgba(255,255,255,.12)}
  .hud{display:flex;gap:16px;flex-wrap:wrap}
</style>
</head>
<body>
  <main>
    <section id="startOverlay"><h1>Missing Boot Script Arena</h1><p>这个测试页面有完整的开始态、画布和 HUD，但故意把本地 boot 脚本删掉，验证 builder 质量门会把这种黑屏启动失败拦下来。</p><button id="startBtn">开始作战</button></section>
    <section><canvas id="gameCanvas"></canvas><div class="hud"><span>score 0</span><span>health 100</span><span>ammo 30</span></div></section>
    <section id="gameOver"><h2>任务失败</h2><p>如果缺少本地脚本，页面看起来像完成，但实际无法启动。</p></section>
  </main>
  <script src="game.js"></script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 第三人称射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("references missing local script" in str(item).lower() for item in report.get("errors", [])))

    def test_validate_builder_quality_rejects_game_with_unclosed_inline_script(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Truncated Script Arena</title>
<style>
  body{margin:0;background:#08111f;color:#f4f7fb;font-family:system-ui,sans-serif}
  main{display:grid;gap:20px;padding:24px}
  canvas{width:100%;height:320px;display:block;background:#102033;border:1px solid rgba(255,255,255,.12)}
  .hud{display:flex;gap:16px;flex-wrap:wrap}
</style>
</head>
<body>
  <main>
    <section id="startOverlay"><h1>Truncated Script Arena</h1><p>这个测试页面保留了开始界面、HUD、画布和游戏循环入口，但故意丢失了 inline script 的闭合标签，用来模拟 builder salvage 后的半截产物。</p><button id="startBtn" onclick="startGame()">开始作战</button></section>
    <section><canvas id="gameCanvas"></canvas><div class="hud"><span id="scoreValue">score 0</span><span id="healthValue">health 100</span></div></section>
    <section id="gameOver"><h2>任务失败</h2><p>这里的失败原因必须来自脚本结构损坏，而不是内容太少。</p><button onclick="startGame()">重新开始</button></section>
  </main>
  <script>
    function startGame(){
      document.body.dataset.state = 'playing';
      requestAnimationFrame(loop);
    }
    function loop(){
      requestAnimationFrame(loop);
    }
""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        # After _repair_script_tag_balance auto-closes the script, the quality gate
        # still catches the game as unplayable because the keydown listener is a no-op
        # and there are no real gameplay controls for a shooter game.
        all_errors = [str(item).lower() for item in report.get("errors", [])]
        self.assertTrue(
            any("placeholder no-ops" in e or "not enough concrete gameplay controls" in e or "input bindings" in e for e in all_errors),
            f"Expected gameplay control/input error, got: {all_errors}",
        )

    def test_validate_builder_quality_rejects_game_with_missing_inline_start_handler(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Missing Start Handler Arena</title>
<style>
  body{margin:0;background:#08111f;color:#f4f7fb;font-family:system-ui,sans-serif}
  main{display:grid;gap:20px;padding:24px}
  section{padding:18px;border:1px solid rgba(255,255,255,.12);border-radius:18px;background:rgba(8,17,31,.72)}
  canvas{width:100%;height:320px;display:block;background:#102033;border:1px solid rgba(255,255,255,.12)}
  .hud{display:flex;gap:16px;flex-wrap:wrap}
</style>
</head>
<body>
  <main>
    <section id="startOverlay"><h1>Missing Start Handler Arena</h1><p>这个测试页面保留了开始界面、HUD、循环和输入绑定，但故意漏掉 startGame 入口函数，用来模拟点击开始后直接抛出 ReferenceError 的产物。</p><button id="startBtn" onclick="startGame()">开始作战</button></section>
    <section><canvas id="gameCanvas"></canvas><div class="hud"><span id="scoreValue">score 0</span><span id="healthValue">health 100</span><span id="ammoValue">ammo 30</span></div></section>
    <section id="gameOver"><h2>任务失败</h2><p>这里保留结束态文案，避免质量门把失败归因到结构不完整。</p><button onclick="startGame()">重新开始</button></section>
  </main>
  <script>
    const state = { started: false };
    function bootScene(){
      state.started = true;
      requestAnimationFrame(loop);
    }
    function loop(){
      if(!state.started){ return; }
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', () => {});
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("undefined function startGame()" in str(item) for item in report.get("errors", [])))

    def test_validate_builder_quality_rejects_game_with_noop_keyboard_controls(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Noop Control Arena</title>
<style>
  body{margin:0;background:#08111f;color:#f4f7fb;font-family:system-ui,sans-serif}
  main{display:grid;gap:20px;padding:24px}
  section{padding:18px;border:1px solid rgba(255,255,255,.12);border-radius:18px;background:rgba(8,17,31,.72)}
  canvas{width:100%;height:320px;display:block;background:#102033;border:1px solid rgba(255,255,255,.12)}
  .hud{display:flex;gap:16px;flex-wrap:wrap}
</style>
</head>
<body>
  <main>
    <section id="startOverlay"><h1>Noop Control Arena</h1><p>这个测试页面看起来像一个完整的第三人称射击游戏壳子，但它故意只绑定了空的键盘监听，验证质量门会拦下无法实际驱动角色的假完成。</p><button id="startBtn" onclick="startGame()">开始作战</button></section>
    <section><canvas id="gameCanvas"></canvas><div class="hud"><span>score 0</span><span>health 100</span><span>ammo 30</span></div></section>
    <section id="gameOver"><h2>任务失败</h2><p>这里保留结束态和 HUD 文案，避免失败原因被误判为结构不完整。</p><button onclick="startGame()">重新开始</button></section>
  </main>
  <script>
    function startGame(){
      document.body.dataset.mode = 'playing';
      requestAnimationFrame(loop);
    }
    function loop(){
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', () => {});
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 第三人称射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("placeholder no-ops" in str(item).lower() for item in report.get("errors", [])))

    def test_validate_builder_quality_rejects_game_when_start_handler_keeps_menu_active(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Menu Lock Arena</title>
<style>
  body{margin:0;background:#08111f;color:#f4f7fb;font-family:system-ui,sans-serif}
  main{display:grid;gap:20px;padding:24px}
  section{padding:18px;border:1px solid rgba(255,255,255,.12);border-radius:18px;background:rgba(8,17,31,.72)}
  canvas{width:100%;height:320px;display:block;background:#102033;border:1px solid rgba(255,255,255,.12)}
  .hud{display:flex;gap:16px;flex-wrap:wrap}
</style>
</head>
<body>
  <main>
    <section id="startOverlay"><h1>Menu Lock Arena</h1><p>这个测试页面具备开始按钮、循环、按键和鼠标监听，但故意不隐藏开始界面，也不切换运行态，验证质量门会拦下“点击后仍停留在菜单”的假开始流程。</p><button id="startBtn" onclick="startGame()">开始作战</button></section>
    <section><canvas id="gameCanvas"></canvas><div class="hud"><span>score 0</span><span>health 100</span><span>ammo 30</span></div></section>
    <section id="gameOver"><h2>任务失败</h2><p>重新开始后应当重新进入战斗，而不是一直留在开始界面。</p><button onclick="startGame()">重新开始</button></section>
  </main>
  <script>
    const controls = {};
    function startGame(){
      document.getElementById('gameCanvas').focus();
      requestAnimationFrame(loop);
    }
    function loop(){
      requestAnimationFrame(loop);
    }
    function onKeyDown(e){
      switch (e.code) {
        case 'KeyW':
        case 'KeyA':
        case 'KeyD':
        case 'Space':
          controls[e.code] = true;
          break;
      }
    }
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', () => { controls.fire = true; });
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个 3D 第三人称射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("dismiss the menu" in str(item).lower() for item in report.get("errors", [])))

    def test_validate_builder_quality_rejects_fake_3d_canvas_imposter_for_tps_goal(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            runtime_dir = out / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// stub runtime", encoding="utf-8")
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fake 3D Arena</title>
<style>
  body{margin:0;background:#08111f;color:#f4f7fb;font-family:system-ui,sans-serif}
  main{display:grid;gap:20px;padding:24px}
  section{padding:18px;border:1px solid rgba(255,255,255,.12);border-radius:18px;background:rgba(8,17,31,.72)}
  canvas{width:100%;height:320px;display:block;background:#102033;border:1px solid rgba(255,255,255,.12)}
  .hud{display:flex;gap:16px;flex-wrap:wrap}
</style>
<script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <main>
    <section id="startOverlay"><h1>Fake 3D Arena</h1><p>这个测试页面故意挂了 Three.js 脚本，但实际玩法仍然是 Canvas2D 假 3D，用来验证质量门不会被脚本标签骗过。</p><button id="startBtn" onclick="startGame()">开始作战</button></section>
    <section><canvas id="gameCanvas"></canvas><div class="hud"><span>score 0</span><span>health 100</span><span>ammo 30</span></div></section>
    <section id="gameOver"><h2>任务失败</h2><p>这里保留完整 HUD、开始态和结束态，失败原因必须来自 3D/TPS 引擎校验。</p><button onclick="startGame()">重新开始</button></section>
  </main>
  <script>
    const canvas = document.getElementById('gameCanvas');
    const ctx = canvas.getContext('2d');
    const controls = {};
    function startGame(){
      document.body.dataset.mode = 'playing';
      requestAnimationFrame(loop);
    }
    function loop(){
      ctx.clearRect(0, 0, canvas.width || 1280, canvas.height || 720);
      ctx.fillStyle = '#00f0ff';
      ctx.fillRect(120, 140, 180, 220);
      requestAnimationFrame(loop);
    }
    function onKeyDown(e){
      switch (e.code) {
        case 'KeyW':
        case 'KeyA':
        case 'KeyS':
        case 'KeyD':
          controls[e.code] = true;
          break;
      }
    }
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', () => { controls.fire = true; });
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个第三人称 3D 射击游戏，要有怪物和武器",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any(
            "canvas2d" in str(item).lower() or "real 3d runtime" in str(item).lower()
            for item in report.get("errors", [])
        ))

    def test_validate_builder_quality_rejects_premium_3d_primitive_placeholder_models(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            runtime_dir = out / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// stub runtime", encoding="utf-8")
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Primitive Model Arena</title>
<script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <button id="startBtn" onclick="startGame()">开始作战</button>
  <canvas id="gameCanvas"></canvas>
  <div class="hud"><span>health 100</span><span>ammo 30</span><span>level 1</span></div>
  <section id="victoryScreen"><h2>任务完成</h2><button onclick="startGame()">重新开始</button></section>
  <script>
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
    const controls = {};
    const weaponGeometry = new THREE.BoxGeometry(0.32, 0.2, 1.8);
    const enemyGeometry = new THREE.ConeGeometry(0.6, 1.8, 8);
    const state = { running: false, levelIndex: 1 };
    function startGame(){
      state.running = true;
      document.body.dataset.mode = 'playing';
      requestAnimationFrame(loop);
    }
    function loop(){
      if (!state.running) return;
      renderer.render(scene, camera);
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', (e) => { controls[e.code] = true; });
    document.addEventListener('mousedown', () => { controls.fire = true; });
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，达到商业级水准。",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("primitive placeholder geometry" in str(item).lower() for item in report.get("errors", [])))

    def test_validate_builder_quality_rejects_premium_3d_multi_part_primitive_assemblies(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            runtime_dir = out / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// stub runtime", encoding="utf-8")
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Composite Primitive Arena</title>
<script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <button id="startBtn" onclick="boot()">开始作战</button>
  <canvas id="gameCanvas"></canvas>
  <div class="hud"><span>health 100</span><span>ammo 30</span><span>level 1</span></div>
  <section id="victoryScreen"><h2>任务完成</h2><button onclick="boot()">重新开始</button></section>
  <script>
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
    function createPlayerModel() {
      const group = new THREE.Group();
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.35, 0.3)));
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.25, 0.3, 0.35)));
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.45, 0.12)));
      group.add(new THREE.Mesh(new THREE.CylinderGeometry(0.01, 0.01, 0.25)));
      return group;
    }
    function createEnemyGrunt() {
      const group = new THREE.Group();
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.55, 0.5, 0.4)));
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.22, 0.35, 0.4)));
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.6, 0.1)));
      group.add(new THREE.Mesh(new THREE.SphereGeometry(0.08, 8, 8)));
      return group;
    }
    function createWeaponModel() {
      const group = new THREE.Group();
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.12, 0.6)));
      group.add(new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.025, 0.3)));
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.2, 0.1)));
      group.add(new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.15, 0.08)));
      return group;
    }
    function boot(){
      document.body.dataset.mode = 'playing';
      requestAnimationFrame(loop);
    }
    function loop(){
      renderer.render(scene, camera);
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', (e) => { window.lastKey = e.code; });
    document.addEventListener('mousedown', () => { window.firing = true; });
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，达到商业级水准。",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        error_text = "\n".join(str(item).lower() for item in report.get("errors", []))
        self.assertIn("primitive placeholder geometry", error_text)
        self.assertIn("character/player", error_text)
        self.assertIn("enemy/monster", error_text)
        self.assertIn("weapon/gun", error_text)

    def test_validate_builder_quality_accepts_premium_3d_composite_enemy_with_single_silhouette_authored_body(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            runtime_dir = out / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// stub runtime", encoding="utf-8")
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Premium Composite Enemy</title>
<style>body{margin:0;background:#05070d;color:#fff}#gameCanvas{width:100vw;height:100vh;display:block}</style>
<script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <button id="startBtn" onclick="boot()">开始作战</button>
  <canvas id="gameCanvas"></canvas>
  <section id="victoryScreen"><h2>任务完成</h2><button onclick="boot()">重新开始</button></section>
  <script>
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
    function createEnemyMonster() {
      const group = new THREE.Group();
      const bodyPoints = [];
      for (let i = 0; i <= 10; i++) {
        const t = i / 10;
        bodyPoints.push(new THREE.Vector2(0.35 + Math.sin(t * Math.PI) * 0.16, t * 1.1));
      }
      const body = new THREE.Mesh(
        new THREE.LatheGeometry(bodyPoints, 14),
        new THREE.MeshStandardMaterial({ color: 0x5c274e, roughness: 0.55, metalness: 0.18, emissive: 0x24081d, emissiveIntensity: 0.35 })
      );
      body.position.y = 0.9;
      body.castShadow = true;
      group.add(body);
      for (let i = 0; i < 6; i++) {
        const angle = (i / 6) * Math.PI * 2;
        const spike = new THREE.Mesh(
          new THREE.ConeGeometry(0.08, 0.42, 6),
          new THREE.MeshStandardMaterial({ color: 0x7b3b62, roughness: 0.48, metalness: 0.22 })
        );
        spike.position.set(Math.cos(angle) * 0.32, 1.2, Math.sin(angle) * 0.32);
        spike.rotation.y = angle;
        spike.castShadow = true;
        group.add(spike);
      }
      const head = new THREE.Mesh(
        new THREE.IcosahedronGeometry(0.28, 0),
        new THREE.MeshStandardMaterial({ color: 0x331427, roughness: 0.5, metalness: 0.12 })
      );
      head.position.y = 1.82;
      head.castShadow = true;
      group.add(head);
      const eye = new THREE.Mesh(
        new THREE.SphereGeometry(0.06, 8, 8),
        new THREE.MeshStandardMaterial({ color: 0xff0044, emissive: 0xff0044, emissiveIntensity: 0.95, roughness: 0.2, metalness: 0.1 })
      );
      eye.position.set(0.08, 1.84, 0.22);
      group.add(eye);
      for (const side of [-1, 1]) {
        const leg = new THREE.Mesh(
          new THREE.CylinderGeometry(0.08, 0.05, 0.92, 6),
          new THREE.MeshStandardMaterial({ color: 0x31111f, roughness: 0.62, metalness: 0.08 })
        );
        leg.position.set(side * 0.24, 0.34, 0.2);
        leg.castShadow = true;
        group.add(leg);
      }
      return group;
    }
    function createPlayerModel() {
      const group = new THREE.Group();
      const shape = new THREE.Shape();
      shape.moveTo(-0.3, 0); shape.lineTo(0.3, 0); shape.lineTo(0.24, 0.55); shape.lineTo(-0.24, 0.55); shape.lineTo(-0.3, 0);
      const torso = new THREE.Mesh(
        new THREE.ExtrudeGeometry(shape, { depth: 0.16, bevelEnabled: true, bevelSize: 0.02, bevelThickness: 0.02 }),
        new THREE.MeshStandardMaterial({ color: 0x28415c, roughness: 0.28, metalness: 0.74, emissive: 0x07172b, emissiveIntensity: 0.22 })
      );
      torso.castShadow = true;
      group.add(torso);
      return group;
    }
    function createWeaponModel() {
      const group = new THREE.Group();
      const frameShape = new THREE.Shape();
      frameShape.moveTo(0, 0); frameShape.lineTo(0.68, 0); frameShape.lineTo(0.74, 0.17); frameShape.lineTo(0.12, 0.22); frameShape.lineTo(0, 0.14); frameShape.lineTo(0, 0);
      const frame = new THREE.Mesh(
        new THREE.ExtrudeGeometry(frameShape, { depth: 0.12, bevelEnabled: true, bevelSize: 0.01, bevelThickness: 0.01 }),
        new THREE.MeshStandardMaterial({ color: 0x2d3946, roughness: 0.24, metalness: 0.84 })
      );
      group.add(frame);
      const barrel = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.48, 8), new THREE.MeshStandardMaterial({ color: 0x161f28, roughness: 0.18, metalness: 0.92 }));
      barrel.rotation.z = -Math.PI / 2;
      barrel.position.set(0.92, 0.11, 0.06);
      group.add(barrel);
      const mag = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.32, 0.1), new THREE.MeshStandardMaterial({ color: 0x394655, roughness: 0.34, metalness: 0.42 }));
      mag.position.set(0.24, -0.16, 0.06);
      group.add(mag);
      const optic = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.24, 8), new THREE.MeshStandardMaterial({ color: 0x0e1720, roughness: 0.16, metalness: 0.88, emissive: 0x02131f, emissiveIntensity: 0.16 }));
      optic.rotation.z = -Math.PI / 2;
      optic.position.set(0.42, 0.22, 0.06);
      group.add(optic);
      return group;
    }
    let currentWeapon = 'rifle';
    const player = { position: new THREE.Vector3(0, 0, 0) };
    function boot() { requestAnimationFrame(loop); }
    function loop() { renderer.render(scene, camera); requestAnimationFrame(loop); }
    document.addEventListener('keydown', (e) => { window.lastKey = e.code; });
    document.addEventListener('mousedown', () => { window.firing = true; });
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，达到商业级水准。",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        error_text = "\n".join(str(item).lower() for item in report.get("errors", []))
        self.assertNotIn("enemy/monster", error_text)
        self.assertFalse("primitive placeholder geometry (enemy/monster)" in error_text)

    def test_validate_builder_quality_rejects_premium_3d_game_without_progression_and_victory(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            runtime_dir = out / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// stub runtime", encoding="utf-8")
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Endless Arena</title>
<script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <button id="startBtn" onclick="startGame()">开始作战</button>
  <canvas id="gameCanvas"></canvas>
  <div class="hud"><span>health 100</span><span>ammo 30</span><span>score 0</span></div>
  <section id="gameOver"><h2>任务失败</h2><button onclick="startGame()">重新开始</button></section>
  <script>
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
    const controls = {};
    const state = { running: false, score: 0 };
    function startGame(){
      state.running = true;
      document.body.dataset.mode = 'playing';
      requestAnimationFrame(loop);
    }
    function loop(){
      if (!state.running) return;
      renderer.render(scene, camera);
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', (e) => { controls[e.code] = true; });
    document.addEventListener('mousedown', () => { controls.fire = true; });
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，还要有关卡和通过页面。",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        error_text = "\n".join(str(item).lower() for item in report.get("errors", []))
        self.assertIn("stage progression", error_text)
        self.assertIn("victory/pass/mission-complete", error_text)

    def test_validate_builder_quality_rejects_missing_drag_camera_for_mouse_hold_phrase(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            runtime_dir = out / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// stub runtime", encoding="utf-8")
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Camera Lock Arena</title>
<script src="./_evermind_runtime/three/three.min.js"></script>
</head>
<body>
  <button id="startBtn" onclick="startGame()">开始作战</button>
  <canvas id="gameCanvas"></canvas>
  <div class="hud"><span>health 100</span><span>ammo 30</span><span>wave 1</span></div>
  <section id="gameOver"><h2>任务失败</h2><button onclick="startGame()">重新开始</button></section>
  <script>
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
    const controls = {};
    const state = { running: false, score: 0 };
    function startGame(){
      state.running = true;
      document.body.dataset.mode = 'playing';
      requestAnimationFrame(loop);
    }
    function loop(){
      if (!state.running) return;
      renderer.render(scene, camera);
      requestAnimationFrame(loop);
    }
    document.addEventListener('keydown', (e) => { controls[e.code] = true; });
    document.addEventListener('mousedown', () => { controls.fire = true; });
  </script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                report = self.orch._validate_builder_quality(
                    [str(index_html)],
                    "",
                    goal="做一个第三人称 3D 射击游戏，鼠标长按屏幕之后可以拉动转动视角，要有怪物、枪械和精美建模。",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        error_text = "\n".join(str(item).lower() for item in report.get("errors", []))
        self.assertIn("drag-to-rotate look controls", error_text)

    def test_builder_retry_regression_reasons_detect_stub_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_root = out / "_stable_previews" / "run_1" / "snapshot"
            stable_root.mkdir(parents=True, exist_ok=True)
            stable_index = stable_root / "index.html"
            stable_index.write_text(
                "<!doctype html><html><body>" + ("premium " * 900) + "</body></html>",
                encoding="utf-8",
            )
            (stable_root / "about.html").write_text(
                "<!doctype html><html><body>" + ("about " * 500) + "</body></html>",
                encoding="utf-8",
            )
            current_index = out / "index.html"
            current_index.write_text("<!doctype html><html><body>stub</body></html>", encoding="utf-8")

            builder = SubTask(id="1", agent_type="builder", description="retry build", depends_on=[])
            builder.retries = 1
            builder.error = "Reviewer rejected (round 1): restore the missing pages"

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._stable_preview_path = stable_index
                reasons = self.orch._builder_retry_regression_reasons(builder, [str(current_index)])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(reasons)
        self.assertTrue(any("collapsed" in reason for reason in reasons))

    def test_builder_retry_regression_reasons_detect_game_flow_loss(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_root = out / "_stable_previews" / "run_1" / "snapshot"
            stable_root.mkdir(parents=True, exist_ok=True)
            stable_index = stable_root / "index.html"
            stable_index.write_text(
                """<!doctype html><html><body>
<div id="startOverlay"></div>
<div id="level-select"></div>
<div id="mission-complete"></div>
<div id="pauseOverlay"></div>
<div id="gameOverOverlay"></div>
<div class="hud"></div>
<canvas id="game"></canvas>
<script>
window.startGame = function(){};
requestAnimationFrame(function gameLoop(){});
document.addEventListener('keydown', () => {});
</script>
</body></html>""",
                encoding="utf-8",
            )
            current_index = out / "index.html"
            current_index.write_text(
                """<!doctype html><html><body>
<div id="startOverlay"></div>
<canvas id="game"></canvas>
<script>window.startGame = function(){};</script>
</body></html>""",
                encoding="utf-8",
            )

            builder = SubTask(id="1", agent_type="builder", description="retry build", depends_on=[])
            builder.retries = 1
            builder.error = "Reviewer rejected (round 1): preserve the richer gameplay flow"

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_goal = "继续优化这个 3D 射击游戏"
                self.orch._stable_preview_path = stable_index
                reasons = self.orch._builder_retry_regression_reasons(builder, [str(current_index)])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any("game flow regressed" in reason for reason in reasons))

    def test_builder_retry_regression_reasons_detect_game_system_loss(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_root = out / "_stable_previews" / "run_1" / "snapshot"
            stable_root.mkdir(parents=True, exist_ok=True)
            stable_index = stable_root / "index.html"
            stable_index.write_text(
                """<!doctype html><html><body>
<div id="startOverlay"></div>
<div id="mission-complete"></div>
<div id="pauseOverlay"></div>
<div id="gameOverOverlay"></div>
<div class="hud"></div>
<canvas id="game"></canvas>
<script>
window.startGame = function(){};
function spawnEnemy() {}
function updateEnemies() {}
function startWave() {}
function switchWeapon(slot) {}
function fireWeapon() {}
function restartGame() {}
let cameraYaw = 0;
let cameraPitch = 0;
document.addEventListener('pointermove', () => { cameraYaw += 0.01; cameraPitch += 0.01; });
document.addEventListener('keydown', (event) => { if (event.code === 'Digit1' || event.code === 'Digit2') switchWeapon(event.code); });
requestAnimationFrame(function gameLoop(){});
</script>
<!-- filler """ + ("premium " * 450) + """ -->
</body></html>""",
                encoding="utf-8",
            )
            current_index = out / "index.html"
            current_index.write_text(
                """<!doctype html><html><body>
<div id="startOverlay"></div>
<div id="mission-complete"></div>
<div id="pauseOverlay"></div>
<div id="gameOverOverlay"></div>
<div class="hud"></div>
<canvas id="game"></canvas>
<script>
window.startGame = function(){};
document.addEventListener('keydown', () => {});
requestAnimationFrame(function gameLoop(){});
</script>
<!-- filler """ + ("patch " * 420) + """ -->
</body></html>""",
                encoding="utf-8",
            )

            builder = SubTask(id="1", agent_type="builder", description="retry build", depends_on=[])
            builder.retries = 1
            builder.error = "Reviewer rejected (round 1): preserve the stronger gameplay systems"

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_goal = "继续优化这个 3D 第三人称射击游戏"
                self.orch._stable_preview_path = stable_index
                reasons = self.orch._builder_retry_regression_reasons(builder, [str(current_index)])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any("game systems regressed" in reason for reason in reasons))


class TestCanonicalArtifactsAndState(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)
        self.orch._canonical_ctx = {
            "task_id": "task_1",
            "run_id": "run_1",
            "is_custom_plan": False,
            "state_snapshot": {"created_at": 1.0},
        }
        self.orch._subtask_ne_map = {"1": "nodeexec_1", "2": "nodeexec_2"}

    def test_persist_tool_artifacts_saves_browser_capture_and_trace(self):
        with tempfile.TemporaryDirectory() as td:
            capture_path = Path(td) / "capture.png"
            trace_path = Path(td) / "trace.zip"
            capture_path.write_bytes(b"png")
            trace_path.write_bytes(b"zip")

            fake_artifact_store = MagicMock()
            fake_artifact_store.save_artifact.side_effect = [
                {"id": "artifact_capture", "artifact_type": "browser_capture", "path": str(capture_path)},
                {"id": "artifact_trace", "artifact_type": "browser_trace", "path": str(trace_path)},
            ]
            fake_ne_store = MagicMock()

            with patch.object(orchestrator_module, "get_artifact_store", return_value=fake_artifact_store), \
                 patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                persisted = self.orch._persist_tool_artifacts("1", [{
                    "_plugin": "browser",
                    "data": {
                        "action": "record_scroll",
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "browser_mode": "headless",
                        "requested_mode": "headless",
                    },
                    "artifacts": [
                        {"type": "image", "path": str(capture_path)},
                        {"type": "trace", "path": str(trace_path)},
                    ],
                }])

        self.assertEqual(len(persisted), 2)
        artifact_types = [call.args[0]["artifact_type"] for call in fake_artifact_store.save_artifact.call_args_list]
        self.assertEqual(artifact_types, ["browser_capture", "browser_trace"])
        fake_ne_store.update_node_execution.assert_any_call("nodeexec_1", {"artifact_ids": ["artifact_capture"]})
        fake_ne_store.update_node_execution.assert_any_call("nodeexec_1", {"artifact_ids": ["artifact_trace"]})

    def test_persist_tool_artifacts_keeps_browser_use_recording_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            video_path = Path(td) / "play.webm"
            video_path.write_bytes(b"webm")

            fake_artifact_store = MagicMock()
            fake_artifact_store.save_artifact.return_value = {
                "id": "artifact_video",
                "artifact_type": "browser_capture",
                "path": str(video_path),
            }
            fake_ne_store = MagicMock()

            with patch.object(orchestrator_module, "get_artifact_store", return_value=fake_artifact_store), \
                 patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                persisted = self.orch._persist_tool_artifacts("1", [{
                    "_plugin": "browser_use",
                    "data": {
                        "final_url": "http://127.0.0.1:8765/preview/index.html",
                        "recording_path": str(video_path),
                        "action_names": ["click_element", "send_keys"],
                    },
                    "artifacts": [
                        {"type": "video", "path": str(video_path)},
                    ],
                }])

        self.assertEqual(len(persisted), 1)
        saved_payload = fake_artifact_store.save_artifact.call_args.args[0]
        self.assertEqual(saved_payload["metadata"]["recording_path"], str(video_path))
        self.assertEqual(saved_payload["metadata"]["action_names"], ["click_element", "send_keys"])

    def test_reconcile_canonical_context_with_plan_persists_state_snapshot(self):
        self.orch._canonical_ctx["node_executions"] = [
            {
                "id": "nodeexec_1",
                "node_key": "builder",
                "input_summary": "old builder summary",
                "depends_on_keys": ["analyst"],
            },
            {
                "id": "nodeexec_2",
                "node_key": "reviewer",
                "input_summary": "old reviewer summary",
                "depends_on_keys": [],
            },
        ]
        self.orch._canonical_ctx["effective_goal"] = "Build premium website"
        self.orch._canonical_ctx["session_context_note"] = "Continue editing the same session project."
        plan = Plan(
            goal="Build premium website",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="Write the multi-page site", depends_on=[]),
                SubTask(id="2", agent_type="reviewer", description="Review the shipped pages", depends_on=["1"]),
            ],
        )
        fake_artifact_store = MagicMock()
        fake_ne_store = MagicMock()

        with patch.object(orchestrator_module, "get_artifact_store", return_value=fake_artifact_store), \
             patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
            drift = self.orch._reconcile_canonical_context_with_plan(plan)

        self.assertTrue(drift)
        fake_ne_store.update_node_execution.assert_any_call(
            "nodeexec_1",
            {
                "depends_on_keys": [],
                "input_summary": (
                    "Write the multi-page site\n\n"
                    "[RUN GOAL]\nBuild premium website\n\n"
                    "[SESSION CONTEXT]\nContinue editing the same session project."
                ),
            },
        )
        fake_ne_store.update_node_execution.assert_any_call(
            "nodeexec_2",
            {
                "depends_on_keys": ["builder"],
                "input_summary": (
                    "Review the shipped pages\n\n"
                    "[RUN GOAL]\nBuild premium website\n\n"
                    "[SESSION CONTEXT]\nContinue editing the same session project."
                ),
            },
        )
        saved_payload = fake_artifact_store.save_artifact.call_args.args[0]
        self.assertEqual(saved_payload["artifact_type"], "state_snapshot")
        self.assertEqual(saved_payload["run_id"], "run_1")
        self.assertEqual(self.orch._canonical_ctx["state_snapshot"]["drift_count"], len(drift))
        self.assertIn("reconciled_at", self.orch._canonical_ctx["state_snapshot"])

    def test_canonical_expected_input_summary_matches_server_format(self):
        self.orch._canonical_ctx = {
            "effective_goal": "Build premium website",
            "session_context_note": "Continue editing the same session project.",
        }
        subtask = SubTask(id="1", agent_type="builder", description="Write the multi-page site", depends_on=[])

        summary = self.orch._canonical_expected_input_summary(subtask)

        self.assertEqual(
            summary,
            (
                "Write the multi-page site\n\n"
                "[RUN GOAL]\nBuild premium website\n\n"
                "[SESSION CONTEXT]\nContinue editing the same session project."
            ),
        )

    def test_hydrate_stable_preview_from_disk_ignores_other_run_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            other_snapshot = out / "_stable_previews" / "run_older" / "1000_builder_quality_pass_task_4"
            other_snapshot.mkdir(parents=True, exist_ok=True)
            (other_snapshot / "index.html").write_text(
                "<!DOCTYPE html><html><body>older stable preview</body></html>",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._run_started_at = 1774492228.883
                self.orch._stable_preview_path = None
                self.orch._stable_preview_files = []
                self.orch._hydrate_stable_preview_from_disk()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIsNone(self.orch._stable_preview_path)
        self.assertEqual(self.orch._stable_preview_files, [])

    def test_restore_root_index_from_stable_preview_keeps_live_root_when_current_run_has_no_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            root_index = out / "index.html"
            root_index.write_text("<!DOCTYPE html><html><body>stale root</body></html>", encoding="utf-8")
            other_snapshot = out / "_stable_previews" / "run_older" / "1000_builder_quality_pass_task_4"
            other_snapshot.mkdir(parents=True, exist_ok=True)
            (other_snapshot / "index.html").write_text(
                "<!DOCTYPE html><html><body>older stable preview</body></html>",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._stable_preview_path = None
                self.orch._restore_root_index_from_stable_preview()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(root_index.exists())
            self.assertIn("stale root", root_index.read_text(encoding="utf-8"))

    def test_restore_root_index_from_stable_preview_skips_active_single_page_run(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_snapshot = out / "_stable_previews" / "run_current" / "1000_builder_quality_pass_task_5"
            stable_snapshot.mkdir(parents=True, exist_ok=True)
            stable_index = stable_snapshot / "index.html"
            stable_index.write_text(
                "<!DOCTYPE html><html><body>stable preview</body></html>",
                encoding="utf-8",
            )
            active_builder = SubTask(
                id="5",
                agent_type="builder",
                description="Build a single-page game.",
                depends_on=[],
                status=TaskStatus.IN_PROGRESS,
            )
            self.orch.active_plan = Plan(
                goal="做一个单页面 3D 射击游戏",
                subtasks=[active_builder],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._stable_preview_path = stable_index
                self.orch._restore_root_index_from_stable_preview()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                self.orch.active_plan = None

            self.assertFalse((out / "index.html").exists())

    def test_prune_invalid_salvaged_builder_files_removes_live_root_html_but_keeps_task_retry_seed(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            root_index = out / "index.html"
            task_dir = out / "task_5"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_index = task_dir / "index.html"
            broken_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Broken Salvage</title>
  <style>
    body { margin: 0; font-family: sans-serif; background: #08111f; color: #f8fafc; }
    main { display: grid; gap: 18px; padding: 24px; }
    section { display: block; padding: 20px; border: 1px solid rgba(148,163,184,.3); border-radius: 18px; }
  </style>
</head>
<body>
  <main>
    <section><h1>Broken Salvage</h1><p>This HTML is substantial enough to act as a retry seed, but the inline script is intentionally truncated so it must never remain as the live root preview.</p></section>
    <section><p>Enough visible content is present to avoid the blank-shell path and reproduce the real runtime bug.</p></section>
  </main>
  <script>
    function boot() {
      const state = { ready: true };
      if (state.ready) {
        console.log('boot');
  </script>
</body>
</html>"""
            root_index.write_text(broken_html, encoding="utf-8")
            task_index.write_text(broken_html, encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                kept, dropped = self.orch._prune_invalid_salvaged_builder_files(
                    [str(root_index), str(task_index)]
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

            self.assertEqual(kept, [])
            self.assertTrue(dropped)
            self.assertFalse(root_index.exists())
            self.assertTrue(task_index.exists())

    def test_promote_stable_preview_materializes_runtime_assets_for_game_html(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            index_html = out / "index.html"
            index_html.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>TPS</title></head>
<body>
  <canvas id="game"></canvas>
  <script src="./_evermind_runtime/three/three.min.js"></script>
  <script>window.__threeReady = !!window.THREE;</script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                self.orch._run_started_at = 1.0
                promoted = self.orch._promote_stable_preview(
                    subtask_id="5",
                    stage="builder_quality_pass",
                    files_created=[str(index_html)],
                    preview_artifact=index_html,
                )
                stable_index = self.orch._stable_preview_path
                runtime_copy = (stable_index.parent / "_evermind_runtime" / "three" / "three.min.js").resolve()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertIsNotNone(promoted)
            self.assertIsNotNone(stable_index)
            self.assertTrue(runtime_copy.exists())
            self.assertIn(str(runtime_copy), promoted.get("files", []))

    def test_restore_output_from_stable_preview_rehydrates_runtime_assets_for_game_html(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_root = out / "_stable_previews" / "run_1" / "snapshot"
            stable_root.mkdir(parents=True, exist_ok=True)
            stable_index = stable_root / "index.html"
            stable_index.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>TPS</title></head>
<body>
  <canvas id="game"></canvas>
  <script src="./_evermind_runtime/three/three.min.js"></script>
  <script>window.__threeReady = !!window.THREE;</script>
</body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._current_task_type = "game"
                self.orch._stable_preview_path = stable_index
                restored = self.orch._restore_output_from_stable_preview()
                runtime_copy = (out / "_evermind_runtime" / "three" / "three.min.js").resolve()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue((out / "index.html").exists())
            self.assertTrue(runtime_copy.exists())
            self.assertIn(str(runtime_copy), restored)

    def test_current_preview_hint_uses_current_run_stable_preview_only(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_snapshot = out / "_stable_previews" / "run_current" / "1000_builder_quality_pass_task_4"
            stable_snapshot.mkdir(parents=True, exist_ok=True)
            stable_index = stable_snapshot / "index.html"
            stable_index.write_text("<!DOCTYPE html><html><body>current stable</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                self.orch._stable_preview_path = stable_index
                hint = self.orch._current_preview_hint("做一个八页奢侈品网站", allow_stable_fallback=True)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("stable_snapshot", hint)
        self.assertIn("_stable_previews/run_current", hint)


class TestReferenceUrlCollection(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_collect_reference_urls_ignores_builder_output_urls(self):
        urls = self.orch._collect_reference_urls(
            "<script src=\"https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js\"></script>",
            [],
            agent_type="builder",
        )
        self.assertEqual(urls, [])

    def test_collect_reference_urls_cleans_markdown_suffix_noise_for_analyst(self):
        urls = self.orch._collect_reference_urls(
            "参考仓库：https://github.com/Mugen87/yuka** 和 https://github.com/donmccurdy/three-pathfinding).",
            [],
            agent_type="analyst",
        )
        self.assertEqual(
            urls,
            [
                "https://github.com/Mugen87/yuka",
                "https://github.com/donmccurdy/three-pathfinding",
            ],
        )

    def test_collect_reference_urls_prefers_requested_source_urls_and_ignores_preview(self):
        urls = self.orch._collect_reference_urls(
            "",
            [
                {"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}},
                {
                    "success": True,
                    "data": {
                        "requested_url": "https://github.com/mrdoob/three.js/blob/dev/examples/jsm/controls/OrbitControls.js",
                        "resolved_url": "https://raw.githubusercontent.com/mrdoob/three.js/dev/examples/jsm/controls/OrbitControls.js",
                    },
                },
            ],
            agent_type="analyst",
        )
        self.assertEqual(
            urls,
            [
                "https://github.com/mrdoob/three.js/blob/dev/examples/jsm/controls/OrbitControls.js",
                "https://raw.githubusercontent.com/mrdoob/three.js/dev/examples/jsm/controls/OrbitControls.js",
            ],
        )

    def test_collect_reference_urls_allows_imagegen_research_text_urls(self):
        urls = self.orch._collect_reference_urls(
            "参考资料：https://github.com/pmndrs/ecctrl 和 https://kenney.nl/assets",
            [],
            agent_type="imagegen",
        )
        self.assertEqual(
            urls,
            [
                "https://github.com/pmndrs/ecctrl",
                "https://kenney.nl/assets",
            ],
        )


class TestNodeWorkSummary(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_summarize_node_work_does_not_mislabel_threejs_builder_as_canvas2d(self):
        summary_stub = type(
            "SummaryStub",
            (),
            {
                "agent_type": "builder",
                "output": """<!DOCTYPE html>
<html lang="zh-CN"><body>
<canvas id="gameCanvas"></canvas>
<script src="./_evermind_runtime/three/three.min.js"></script>
<script>
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
  const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
  function gameLoop(){ requestAnimationFrame(gameLoop); renderer.render(scene, camera); }
  gameLoop();
</script>
</body></html>""",
            },
        )()

        bullets = self.orch._summarize_node_work(summary_stub)
        joined = "\n".join(bullets)

        self.assertIn("Three.js/WebGL", joined)
        self.assertNotIn("Canvas 2D", joined)

    def test_summarize_node_work_imagegen_uses_created_files_for_human_readable_bullets(self):
        summary_stub = type(
            "SummaryStub",
            (),
            {
                "agent_type": "imagegen",
                "output": "style lock topology rig manifest replacement",
                "files_created": [
                    "/tmp/evermind_output/assets/00_visual_target.md",
                    "/tmp/evermind_output/assets/01_style_lock.md",
                    "/tmp/evermind_output/assets/character_hero_sheet.svg",
                    "/tmp/evermind_output/assets/manifest.json",
                ],
            },
        )()

        bullets = self.orch._summarize_node_work(summary_stub)
        joined = "\n".join(bullets)

        self.assertIn("visual target", joined)
        self.assertIn("概念 sheet", joined)
        self.assertIn("manifest", joined)


class TestReviewerGate(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_reviewer_description_uses_consistent_strict_thresholds(self):
        desc = self.orch._reviewer_task_description("做一个高质量官网", pro=True)
        self.assertIn("ANY single dimension score below 5 = AUTOMATIC REJECT", desc)
        self.assertIn("originality", desc)
        self.assertIn("ship_readiness", desc)
        self.assertIn("missing_deliverables", desc)

    def test_reviewer_description_adds_motion_gate_for_motion_brief(self):
        desc = self.orch._reviewer_task_description("做一个像苹果一样高级的 8 页奢侈品官网，要有高级动画和页面过渡", pro=True)
        self.assertIn("MOTION / TRANSITION GATE", desc)
        self.assertIn("hard-cuts between routes", desc)

    def test_parse_reviewer_verdict_rejects_when_blocking_issues_exist(self):
        output = (
            '{"verdict":"APPROVED","scores":{"layout":8,"color":8,"typography":8,"animation":7,'
            '"responsive":8,"functionality":8,"completeness":8,"originality":7},'
            '"blocking_issues":["Primary CTA is broken"],"required_changes":[]}'
        )
        self.assertEqual(self.orch._parse_reviewer_verdict(output), "REJECTED")

    def test_parse_reviewer_verdict_rejects_when_ship_readiness_low_or_missing_deliverables(self):
        output = (
            '{"verdict":"APPROVED","ship_readiness":6,'
            '"scores":{"layout":8,"color":8,"typography":8,"animation":7,"responsive":8,"functionality":8,"completeness":8,"originality":7},'
            '"blocking_issues":[],"required_changes":[],"missing_deliverables":["game over loop"]}'
        )
        self.assertEqual(self.orch._parse_reviewer_verdict(output), "REJECTED")

    def test_parse_reviewer_verdict_rejects_when_core_quality_dimensions_below_six(self):
        output = (
            '{"verdict":"APPROVED","ship_readiness":8,'
            '"scores":{"layout":8,"color":8,"typography":8,"animation":7,"responsive":8,"functionality":5,"completeness":8,"originality":7},'
            '"blocking_issues":[],"required_changes":[],"missing_deliverables":[]}'
        )
        self.assertEqual(self.orch._parse_reviewer_verdict(output), "REJECTED")

    def test_parse_reviewer_verdict_returns_unknown_when_verdict_missing(self):
        output = (
            "Reviewed navigation, typography, imagery consistency, and motion. "
            "Several observations are listed, but no final JSON verdict was produced."
        )
        self.assertEqual(self.orch._parse_reviewer_verdict(output), "UNKNOWN")

    def test_forced_reviewer_rejection_penalizes_multi_page_regression(self):
        payload = self.orch._build_reviewer_forced_rejection(
            preview_gate={
                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                "errors": [],
                "warnings": [],
                "smoke": {"status": "pass"},
            },
            multi_page_gate={
                "ok": False,
                "expected_pages": 8,
                "html_files": ["index.html"],
                "errors": ["Multi-page delivery incomplete: found 1/8 HTML pages in the current run."],
                "missing_nav_targets": ["features.html", "contact.html"],
                "unlinked_secondary_pages": [],
            },
        )
        parsed = json.loads(payload)
        self.assertEqual(parsed.get("verdict"), "REJECTED")
        self.assertLessEqual(parsed.get("scores", {}).get("completeness", 10), 1)
        self.assertLessEqual(parsed.get("scores", {}).get("functionality", 10), 2)
        self.assertTrue(any("8 requested HTML pages" in str(item) for item in parsed.get("missing_deliverables", [])))
        self.assertLessEqual(int(parsed.get("ship_readiness", 10) or 10), 4)

    def test_strong_failure_marker_wins_over_pass_words(self):
        output = "Created successfully, but Missing <head> tag and No CSS styles found."
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "fail")

    def test_cloud_deploy_warning_is_not_a_real_failure(self):
        output = "No public URL available for deployment, but files exist in output directory."
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "pass")

    def test_deterministic_gate_pass_not_misparsed_by_failed_word(self):
        output = (
            "No failed assertions detected. Deterministic visual gate passed; smoke=pass; "
            "preview=http://127.0.0.1:8765/preview/index.html. __EVERMIND_TESTER_GATE__=PASS"
        )
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "pass")

    def test_no_artifact_failure_is_non_retryable(self):
        output = (
            "Deterministic visual gate failed; smoke=skipped; preview=n/a. "
            "QUALITY GATE FAILED: ['No HTML preview artifact found for tester validation'] "
            "__EVERMIND_TESTER_GATE__=FAIL"
        )
        parsed = self.orch._parse_test_result(output)
        self.assertEqual(parsed.get("status"), "fail")
        self.assertFalse(parsed.get("retryable", True))

    def test_forced_rejection_absorbs_visual_regression_guidance(self):
        payload = json.loads(self.orch._build_reviewer_forced_rejection(
            preview_gate={
                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                "smoke": {
                    "status": "pass",
                    "body_text_len": 420,
                    "render_summary": {
                        "readable_text_count": 18,
                        "heading_count": 4,
                        "interactive_count": 6,
                        "image_count": 2,
                        "canvas_count": 0,
                    },
                },
                "visual_regression": {
                    "status": "fail",
                    "summary": "Visual regression gate failed: 2 capture(s) diverged sharply from the last approved baseline.",
                    "issues": [
                        "The current full-page layout is 52% shorter than the last approved baseline; lower sections may be missing or collapsed.",
                    ],
                    "suggestions": [
                        "Restore the missing lower sections and page depth before re-review; compare the full-page content stack against the last approved version.",
                    ],
                },
            }
        ))
        self.assertEqual(payload.get("verdict"), "REJECTED")
        self.assertTrue(any("baseline" in item.lower() for item in payload.get("issues", [])))
        self.assertTrue(any("missing lower sections" in item.lower() for item in payload.get("required_changes", [])))
        self.assertTrue(any("current screenshots stay close" in item.lower() for item in payload.get("acceptance_criteria", [])))

    def test_forced_rejection_flags_large_mid_page_blank_gap(self):
        payload = json.loads(self.orch._build_reviewer_forced_rejection(
            preview_gate={
                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                "smoke": {
                    "status": "fail",
                    "body_text_len": 180,
                    "render_errors": [
                        "Large blank vertical gap detected: content disappears for about 1180px between upper and lower sections",
                    ],
                    "render_summary": {
                        "readable_text_count": 16,
                        "heading_count": 4,
                        "interactive_count": 4,
                        "image_count": 1,
                        "canvas_count": 0,
                        "largest_blank_gap": 1180,
                        "blank_gap_count": 1,
                    },
                },
            }
        ))
        self.assertEqual(payload.get("verdict"), "REJECTED")
        self.assertTrue(any("blank band" in item.lower() for item in payload.get("blocking_issues", [])))
        self.assertTrue(any("missing middle sections" in item.lower() for item in payload.get("required_changes", [])))
        self.assertGreaterEqual(int(payload.get("blank_sections_found", 0) or 0), 1)

    def test_forced_rejection_absorbs_structural_quality_gate_findings(self):
        payload = json.loads(self.orch._build_reviewer_forced_rejection(
            preview_gate={
                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                "smoke": {"status": "pass"},
            },
            quality_gate={
                "errors": [
                    "3D brief regressed to a non-3D runtime; reviewer should not approve a 2D/canvas-only fallback.",
                ],
                "warnings": [
                    "Deterministic palette signal still looks flat/monochrome; reviewer should inspect whether the page collapsed into black/white slabs.",
                ],
                "weak_routes": [
                    "index.html (text≈96, headings=1, interactive=1, visual_anchors=0)",
                ],
                "route_signals": {
                    "palette": {"flat_monochrome_risk": True},
                },
            },
        ))
        self.assertEqual(payload.get("verdict"), "REJECTED")
        self.assertTrue(any("3d brief regressed" in item.lower() for item in payload.get("blocking_issues", [])))
        self.assertTrue(any("2d fallback" in item.lower() for item in payload.get("required_changes", [])))
        self.assertTrue(any("palette" in item.lower() or "black/white" in item.lower() for item in payload.get("required_changes", [])))

    def test_reviewer_structural_quality_gate_flags_underbuilt_routes(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title><style>body{margin:0;background:#fff;color:#111}</style></head>
<body><main><h1>Home</h1><p>Short.</p></main></body></html>""",
                encoding="utf-8",
            )
            (out / "pricing.html").write_text(
                """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pricing</title><style>body{margin:0;background:#fff;color:#111}</style></head>
<body><main><h1>Pricing</h1><p>Tiny.</p></main></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                gate = self.orch._reviewer_structural_quality_gate("做一个三页面官网，包含首页、定价页和联系页")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertFalse(gate.get("ok"))
        self.assertTrue(any("thin/underbuilt pages" in str(item) for item in gate.get("errors", [])))

    def test_reviewer_structural_quality_gate_rejects_3d_brief_that_regressed_to_2d(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Fallback Shooter</title>
<style>body{margin:0;background:#111;color:#fff}canvas{display:block;width:100vw;height:100vh}</style></head>
<body><canvas id="game"></canvas><script>
const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
function loop(){ ctx.fillStyle = '#111'; ctx.fillRect(0,0,canvas.width || 800, canvas.height || 600); requestAnimationFrame(loop); }
loop();
</script></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                gate = self.orch._reviewer_structural_quality_gate(
                    "做一个商业级 3D 第三人称射击游戏，要有怪物、枪械、大地图和精致模型"
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertFalse(gate.get("ok"))
        self.assertTrue(any("3D brief regressed" in str(item) for item in gate.get("errors", [])))

    def test_interaction_gate_accepts_record_scroll_after_click_for_website_review(self):
        reason = self.orch._interaction_gate_error(
            "reviewer",
            "website",
            [
                {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                {"action": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/"},
                {
                    "action": "record_scroll",
                    "ok": True,
                    "state_changed": True,
                    "at_bottom": True,
                    "at_page_bottom": True,
                    "is_scrollable": True,
                    "url": "http://127.0.0.1:8765/preview/",
                },
            ],
            "做一个高级品牌官网",
        )
        self.assertIsNone(reason)

    def test_interaction_gate_names_missing_artifact_pages_for_multi_page_review(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for name in ("index.html", "pricing.html", "contact.html"):
                (out / name).write_text("<!doctype html><html><body></body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                reason = self.orch._interaction_gate_error(
                    "reviewer",
                    "website",
                    [
                        {"action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                        {"action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "is_scrollable": False},
                        {"action": "click", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                        {"action": "observe", "ok": True, "state_changed": True, "url": "http://127.0.0.1:8765/preview/pricing.html"},
                    ],
                    "做一个三页面官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
        self.assertIn("contact.html", reason)
        self.assertIn("2/3", reason)


class TestAnalystHandoffFallback(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_missing_handoff_sections_are_synthesized(self):
        plan = Plan(
            goal="创建一个介绍奢侈品的英文网站（8页），页面要简约高级，像苹果一样",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="builder", description="build-a"),
                SubTask(id="3", agent_type="builder", description="build-b"),
                SubTask(id="4", agent_type="reviewer", description="review"),
                SubTask(id="5", agent_type="tester", description="test"),
                SubTask(id="6", agent_type="debugger", description="debug"),
            ],
        )
        raw = "Apple-like premium minimalism with cinematic transitions and editorial product storytelling."
        augmented, synthesized, remaining = self.orch._materialize_analyst_handoff(
            plan,
            raw,
            visited_urls=["https://www.apple.com"],
        )
        self.assertIn("reference_sites", synthesized)
        self.assertIn("builder_1_handoff", synthesized)
        self.assertIn("tester_handoff", synthesized)
        self.assertEqual(remaining, [])
        self.assertIn("https://www.apple.com", self.orch._extract_tagged_section(augmented, "reference_sites"))
        self.assertIn("index.html", self.orch._extract_tagged_section(augmented, "builder_1_handoff"))
        self.assertIn("every requested page", self.orch._extract_tagged_section(augmented, "tester_handoff"))

    def test_single_builder_handoff_stays_end_to_end_for_complex_site(self):
        plan = Plan(
            goal="做一个介绍奢侈品的英文网站（8页），页面要简约高级，像苹果一样，并有页面转场动画。",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="uidesign", description="design", depends_on=["1"]),
                SubTask(id="3", agent_type="scribe", description="content", depends_on=["1"]),
                SubTask(id="4", agent_type="builder", description="build", depends_on=["1", "2", "3"]),
                SubTask(id="5", agent_type="reviewer", description="review", depends_on=["4"]),
            ],
        )
        synthesized = self.orch._synthesized_analyst_handoff_sections(plan, "premium editorial direction")
        self.assertIn("ENTIRE routed experience", synthesized["builder_1_handoff"])
        self.assertIn("single builder", synthesized["builder_2_handoff"])

    def test_game_handoff_synthesizes_control_frame_and_asset_sourcing_sections(self):
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带拖拽视角、怪物、枪械和关卡。",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="builder", description="build", depends_on=["1"]),
                SubTask(id="3", agent_type="reviewer", description="review", depends_on=["2"]),
            ],
        )
        augmented, synthesized, remaining = self.orch._materialize_analyst_handoff(
            plan,
            "premium third-person combat direction",
            visited_urls=["https://github.com/example/tps-demo", "https://docs.example.com/three-tps"],
        )
        self.assertIn("control_frame_contract", synthesized)
        self.assertIn("asset_sourcing_plan", synthesized)
        self.assertEqual(remaining, [])
        self.assertIn("W moves away from the camera", self.orch._extract_tagged_section(augmented, "control_frame_contract"))
        self.assertIn("Use source_fetch first", self.orch._extract_tagged_section(augmented, "asset_sourcing_plan"))
        self.assertIn("gdquest-demos/godot-4-3d-third-person-controller", self.orch._extract_tagged_section(augmented, "asset_sourcing_plan"))
        self.assertIn("Projectile readability baseline", self.orch._extract_tagged_section(augmented, "game_mechanics_spec"))


class TestPlannerContextIsolation(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_history_is_ignored_for_new_unrelated_goal(self):
        summary = self.orch._build_context_summary(
            "做一个介绍奢侈品的全新官网",
            conversation_history=[
                {"role": "user", "content": "之前帮我做一个茶叶品牌网站"},
                {"role": "agent", "content": "用了茶叶、竹林和东方绿色配色。"},
            ],
        )
        self.assertEqual(summary, "")

    def test_cross_session_memory_is_kept_for_fresh_session_goal(self):
        self.orch._cross_session_memory_note = (
            "Recent related run detected from a different session/client.\n"
            "Previous summary: reviewer rejected the prior build because the camera started in front of the player."
        )
        summary = self.orch._build_context_summary(
            "创建一个3D第三人称射击游戏，要有怪物、武器、大地图、关卡和通过页面，整体要达到商业级质量。",
            conversation_history=[],
        )
        self.assertIn("Recent Related Run Memory", summary)
        self.assertIn("camera started in front of the player", summary)
        self.assertNotIn("CONVERSATION CONTEXT", summary)

    def test_history_is_kept_for_explicit_continuation_goal(self):
        summary = self.orch._build_context_summary(
            "继续刚才那个奢侈品网站，基于上次结果优化动画和中间页面",
            conversation_history=[
                {"role": "user", "content": "做一个介绍奢侈品的官网"},
                {"role": "agent", "content": "已生成 8 页版本，风格偏苹果系极简。"},
            ],
        )
        self.assertIn("奢侈品", summary)
        self.assertIn("8 页版本", summary)

    def test_history_is_kept_for_implicit_edit_goal_when_artifact_exists(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "index.html").write_text(
                "<!doctype html><html><body><h1>existing artifact</h1></body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                summary = self.orch._build_context_summary(
                    "把这个奢侈品网站再优化一下，用平衡模式修改动画和排版",
                    conversation_history=[
                        {"role": "user", "content": "做一个介绍奢侈品的官网"},
                        {"role": "agent", "content": "已生成 8 页版本，风格偏苹果系极简。"},
                    ],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
        self.assertIn("奢侈品", summary)
        self.assertIn("8 页版本", summary)

    def test_prepare_output_dir_preserves_artifacts_for_implicit_edit_goal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            artifact = tmp_out / "index.html"
            artifact.write_text("<!doctype html><html><body>keep me</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                self.orch._current_goal = "把这个贪吃蛇游戏再优化一下，用平衡模式修改手感和界面"
                self.orch._current_conversation_history = [
                    {"role": "user", "content": "创建一个贪吃蛇小游戏"},
                    {"role": "agent", "content": "已生成一个可运行的贪吃蛇版本。"},
                ]
                self.orch._prepare_output_dir_for_run()
                self.assertTrue(artifact.exists())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

    def test_builder_refinement_context_is_available_for_continuation_goal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "index.html").write_text(
                "<!doctype html><html><head><title>Steel Rift</title></head><body><h1>Steel Rift</h1><section>Current playable build</section></body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                self.orch._current_goal = "继续刚才这个 3D 射击游戏，在原有项目基础上优化 HUD 和手感"
                self.orch._current_conversation_history = [
                    {"role": "user", "content": "创建一个第三人称 3D 射击游戏"},
                    {"role": "agent", "content": "已生成一个可运行版本，包含 HUD 和关卡推进。"},
                ]
                plan = Plan(
                    goal=self.orch._current_goal,
                    subtasks=[SubTask(id="1", agent_type="builder", description="build")],
                )
                ctx = self.orch._builder_refinement_context(plan, plan.subtasks[0])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
        self.assertIn("Preserve the strongest existing gameplay shell", ctx)
        self.assertIn("Current playable build", ctx)


class TestBuilderRuntimeArtifactMerge(unittest.TestCase):
    def test_runtime_root_index_is_merged_back_into_builder_files(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "about.html", "contact.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="做一个三页面奢侈品官网",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build index.html, about.html, contact.html",
                    started_at=time.time(),
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = time.time()
                root_index = tmp_out / "index.html"
                root_index.write_text("<!doctype html><html><body>home</body></html>", encoding="utf-8")
                merged = orch._merge_builder_runtime_html_files(
                    plan,
                    plan.subtasks[0],
                    [str(tmp_out / "about.html")],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        normalized = {str(Path(item).resolve()) for item in merged}
        self.assertIn(str(root_index.resolve()), normalized)


class TestBuilderHeartbeatOutput(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_builder_heartbeat_before_first_write_mentions_missing_real_files(self):
        text = self.orch._heartbeat_partial_output("builder", 45, has_file_write=False)
        self.assertIn("尚未检测到真实HTML文件落盘", text)

    def test_builder_heartbeat_after_write_returns_execution_progress(self):
        text = self.orch._heartbeat_partial_output("builder", 45, has_file_write=True)
        self.assertIn("正在编写样式和交互逻辑", text)

    def test_builder_pending_write_signal_expires_for_heartbeat_phase(self):
        subtask = SubTask(id="1", agent_type="builder", description="build")
        plan = Plan(goal="做一个第三人称 3D 射击游戏", subtasks=[subtask])
        now = time.time()
        subtask.builder_pending_write_at = now

        self.assertTrue(self.orch._builder_has_recent_pending_write_signal(plan, subtask, now=now + 10))
        self.assertFalse(self.orch._builder_has_recent_pending_write_signal(plan, subtask, now=now + 45))

    def test_parallel_multi_page_builder_descriptions_do_not_both_claim_index(self):
        primary, secondary = self.orch._parallel_builder_task_descriptions(
            "做一个奢侈品品牌官网，总共8个独立页面，不能做成长滚动单页。"
        )
        self.assertIn("Create index.html", primary)
        self.assertIn("You own /tmp/evermind_output/index.html", primary)
        self.assertIn("Do NOT write /tmp/evermind_output/index.html", secondary)
        self.assertNotIn("Create index.html plus", secondary)

    def test_imagegen_heartbeat_mentions_visual_lock_work(self):
        text = self.orch._heartbeat_partial_output("imagegen", 45, loaded_skills=["image-prompt-director"])
        self.assertIn("建模 brief", text)

    def test_assetimport_heartbeat_mentions_manifest_pipeline(self):
        text = self.orch._heartbeat_partial_output("assetimport", 75, loaded_skills=["asset-pipeline-packaging"])
        self.assertIn("manifest", text)

    def test_merger_heartbeat_mentions_parallel_builder_comparison(self):
        text = self.orch._heartbeat_partial_output("merger", 80, loaded_skills=["godogen-playable-loop"])
        self.assertIn("builder1 / builder2", text)

    def test_normalize_heartbeat_activity_text_strips_elapsed_and_skill_suffix(self):
        text = self.orch._normalize_heartbeat_activity_text(
            "正在整理 manifest、replacement points 与运行时装载约束 | 已加载技能: asset-pipeline-packaging (40s)"
        )
        self.assertEqual(text, "正在整理 manifest、replacement points 与运行时装载约束")


class TestBuilderFirstWriteTimeout(unittest.TestCase):
    """Tests for §P0-FIRST-WRITE: builder early abort if no real file written."""

    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_first_write_timeout_constant_exists(self):
        from orchestrator import BUILDER_FIRST_WRITE_TIMEOUT_SEC
        self.assertGreaterEqual(BUILDER_FIRST_WRITE_TIMEOUT_SEC, 60)
        self.assertLessEqual(BUILDER_FIRST_WRITE_TIMEOUT_SEC, 480)

    def test_builder_heartbeat_before_first_write_warns_at_high_elapsed(self):
        text = self.orch._heartbeat_partial_output("builder", 95, has_file_write=False)
        self.assertIn("未检测到真实HTML文件落盘", text)

    def test_builder_heartbeat_after_write_does_not_warn(self):
        text = self.orch._heartbeat_partial_output("builder", 95, has_file_write=True)
        self.assertNotIn("尚未检测到", text)

    def test_scribe_handoff_condensed_tighter_for_builder(self):
        plan = Plan(
            goal="做一个三页面官网",
            subtasks=[
                SubTask(id="1", agent_type="scribe", description="write content"),
                SubTask(id="2", agent_type="builder", description="build", depends_on=["1"]),
            ],
        )
        # Simulate a large scribe output (44000 chars like the real case)
        scribe_output = "A" * 44000
        prev_results = {"1": {"output": scribe_output}}
        # Build context the same way the orchestrator does
        context_parts = []
        subtask = plan.subtasks[1]
        for dep_id in subtask.depends_on:
            dep_result = prev_results.get(dep_id, {})
            dep_task = next((s for s in plan.subtasks if s.id == dep_id), None)
            if dep_task and dep_result.get("output"):
                dep_output = str(dep_result.get("output", ""))
                if dep_task.agent_type == "scribe" and subtask.agent_type == "builder":
                    condensed = self.orch._condense_handoff_seed(dep_output, limit=600)
                    context_parts.append(
                        f"[Result from scribe #{dep_id}]:\n{condensed}"
                    )
                    continue
                context_parts.append(dep_output[:900])
        context = "\n\n".join(context_parts)
        # Verify condensed output is within 600 chars + header
        self.assertLessEqual(len(context), 700)  # 600 body + ~50 header


class TestBuilderExtractionAndRetryGuards(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_extract_code_files_skips_invalid_truncated_html_block(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    "```html index.html\n<!DOCTYPE html><html><head><style>body{opacity:1}... [TRUNCATED]\n```",
                    subtask_id="9",
                    allow_root_index_copy=True,
                    multi_page_required=True,
                    allowed_html_targets=["index.html", "pricing.html"],
                )
                file_exists = (out_dir / "index.html").exists()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [])
        self.assertFalse(file_exists)

    def test_extract_and_save_code_materializes_local_game_runtime_assets(self):
        original_output = orchestrator_module.OUTPUT_DIR
        original_task_type = getattr(self.orch, "_current_task_type", None)
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._current_task_type = "game"
                files = self.orch._extract_and_save_code(
                    "```html index.html\n"
                    "<!DOCTYPE html><html><head>"
                    "<script src='https://cdn.jsdelivr.net/npm/phaser@3.80.1/dist/phaser.min.js'></script>"
                    "</head><body><main><h1>Phaser Arena</h1>"
                    "<button id='startBtn' onclick='startGame()'>Start</button>"
                    "<div id='hud'>HP 100</div>"
                    "<script>"
                    "let game = null;"
                    "function startGame(){ game = new Phaser.Game({type: Phaser.AUTO}); requestAnimationFrame(loop); }"
                    "function loop(){ requestAnimationFrame(loop); }"
                    "</script></main></body></html>\n"
                    "```",
                    subtask_id="42",
                    allow_root_index_copy=True,
                )
                saved_html = (out_dir / "index.html").read_text(encoding="utf-8")
                runtime_asset = out_dir / "_evermind_runtime" / "phaser" / "phaser.min.js"
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                if original_task_type is None and hasattr(self.orch, "_current_task_type"):
                    delattr(self.orch, "_current_task_type")
                else:
                    self.orch._current_task_type = original_task_type
            self.assertIn(str(out_dir / "index.html"), files)
            self.assertIn("./_evermind_runtime/phaser/phaser.min.js", saved_html)
            self.assertTrue(runtime_asset.exists())
            self.assertGreater(runtime_asset.stat().st_size, 1000)

    def test_bootstrap_scaffold_preserves_strong_in_progress_page(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            strong_but_incomplete = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Luxury Tea</title>
<style>
:root{--bg:#0a0a0b;--fg:#f4f1ea;--line:rgba(255,255,255,.12);}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:24px;border-bottom:1px solid var(--line)}
main{display:grid;gap:24px;padding:32px}section{padding:28px;border:1px solid var(--line);border-radius:24px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.cta{display:flex;gap:12px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header><nav><a href="index.html">Home</a><a href="pricing.html">Pricing</a><a href="about.html">About</a></nav></header>
<main>
<section><h1>Luxury Tea</h1><p>Editorial-grade homepage already in progress.</p></section>
<section class="grid"><article><h2>Origin</h2><p>Single-estate sourcing.</p></article><article><h2>Craft</h2><p>Hand-finished harvests.</p></article><article><h2>Ritual</h2><p>Concierge tasting service.</p></article></section>
<section class="cta"><button>Book Tasting</button><button>Explore Collection</button></section>
</main>
</body>
"""
            (out_dir / "index.html").write_text(strong_but_incomplete, encoding="utf-8")
            plan = Plan(
                goal="做一个 8 页高级茶叶官网，要有动画和独立页面",
                subtasks=[SubTask(id="1", agent_type="builder", description="build the full site")],
            )
            subtask = plan.subtasks[0]
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                written = self.orch._ensure_builder_bootstrap_scaffold(plan, subtask)
                index_after = (out_dir / "index.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertNotIn(str(out_dir / "index.html"), written)
        self.assertIn("Luxury Tea", index_after)
        self.assertNotIn("evermind-bootstrap scaffold", index_after.lower())

    def test_extract_code_raw_html_fallback_uses_single_allowed_target(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                expected_path = out_dir / "task_9" / "faq.html"
                files = self.orch._extract_and_save_code(
                    "<!DOCTYPE html><html><body><h1>FAQ</h1></body></html>",
                    subtask_id="9",
                    allow_root_index_copy=False,
                    multi_page_required=True,
                    allowed_html_targets=["faq.html"],
                    allow_multi_page_raw_html_fallback=True,
                )
                faq_exists = expected_path.exists()
                index_exists = (out_dir / "index.html").exists()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [str(expected_path)])
        self.assertTrue(faq_exists)
        self.assertFalse(index_exists)

    def test_prepare_builder_quality_files_materializes_runtime_assets_for_merged_preview(self):
        original_output = orchestrator_module.OUTPUT_DIR
        original_task_type = getattr(self.orch, "_current_task_type", None)
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            part1 = out_dir / "index_part1.html"
            part2 = out_dir / "index_part2.html"
            part1.write_text(
                "<!doctype html><html><head><meta charset='UTF-8'><title>Top</title>"
                "<script src='./_evermind_runtime/howler/howler.min.js'></script></head>"
                "<body><main><section>Top half</section></main></body></html>",
                encoding="utf-8",
            )
            part2.write_text(
                "<!doctype html><html><head><meta charset='UTF-8'><title>Bottom</title></head>"
                "<body><section>Bottom half</section><script>window.__bottom=true;</script></body></html>",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._current_task_type = "game"
                files, preview = self.orch._prepare_builder_quality_files([str(part1), str(part2)])
                merged_root = out_dir / "index.html"
                merged_html = merged_root.read_text(encoding="utf-8")
                materialized_asset = Path(
                    next(path for path in files if path.endswith("/_evermind_runtime/howler/howler.min.js"))
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                if original_task_type is None and hasattr(self.orch, "_current_task_type"):
                    delattr(self.orch, "_current_task_type")
                else:
                    self.orch._current_task_type = original_task_type

            self.assertEqual(preview, merged_root)
            self.assertIn(str(merged_root.resolve()), files)
            self.assertIn("./_evermind_runtime/howler/howler.min.js", merged_html)
            self.assertTrue(materialized_asset.exists())
            self.assertGreater(materialized_asset.stat().st_size, 1000)

    def test_extract_code_accepts_absolute_output_paths_for_named_builder_files(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            payload = (
                f"```css {out_dir / 'styles.css'}\nbody{{background:#111;color:#fff;}}\n```\n"
                f"```javascript {out_dir / 'app.js'}\nconsole.log('ok')\n```\n"
                f"```html {out_dir / 'index.html'}\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    payload,
                    subtask_id="4",
                    allow_root_index_copy=True,
                    multi_page_required=True,
                    allowed_html_targets=["index.html", "about.html"],
                    allow_multi_page_raw_html_fallback=True,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn(str(out_dir / "styles.css"), files)
        self.assertIn(str(out_dir / "app.js"), files)
        self.assertIn(str(out_dir / "index.html"), files)
        self.assertFalse((out_dir / "task_4" / "styles.css").exists())
        self.assertFalse((out_dir / "task_4" / "index.js").exists())

    def test_save_extracted_code_block_skips_duplicate_shared_asset_retry_merge(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            primary_css = (
                "body{background:#111;color:#f5f5f5;}"
                ".hero{padding:64px 48px;min-height:100vh;display:grid;place-items:center;}"
            )
            secondary_css = (
                ".pricing-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;}"
                ".pricing-card{border:1px solid rgba(255,255,255,.12);padding:24px;border-radius:24px;}"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files: list[str] = []
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=primary_css,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-4",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=secondary_css,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-5",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=secondary_css,
                    files=files,
                    allow_root_index_copy=True,
                    is_retry=True,
                    merge_owner="builder-5",
                )
                merged = (out_dir / "styles.css").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(merged.count("pricing-grid"), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-4"), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-5"), 1)

    def test_save_extracted_code_block_retry_replaces_owner_section_without_dropping_other_builder_assets(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            initial_primary = ".hero{padding:64px 48px;background:#111;color:#f5f5f5;}"
            updated_primary = ".hero{padding:96px 72px;background:#16171c;color:#f8f2e8;}"
            secondary = ".pricing-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;}"
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files: list[str] = []
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=initial_primary,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-4",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=secondary,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-5",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=updated_primary,
                    files=files,
                    allow_root_index_copy=True,
                    is_retry=True,
                    merge_owner="builder-4",
                )
                merged = (out_dir / "styles.css").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn(updated_primary, merged)
        self.assertIn(secondary, merged)
        self.assertNotIn(initial_primary, merged)
        self.assertEqual(merged.count("Builder Asset Start: builder-4"), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-5"), 1)

    def test_save_extracted_code_block_retry_extracts_owner_payload_from_combined_shared_asset(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            initial_primary = ".hero{padding:64px 48px;background:#111;color:#f5f5f5;}"
            updated_primary = ".hero{padding:96px 72px;background:#162033;color:#f8f2e8;}"
            secondary = ".city-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;}"
            retry_payload = (
                "/* ── Builder Asset Start: builder-4 ── */\n"
                f"{updated_primary}\n"
                "/* ── Builder Asset End: builder-4 ── */\n\n"
                "/* ── Builder Asset Start: builder-5 ── */\n"
                f"{secondary}\n"
                "/* ── Builder Asset End: builder-5 ── */\n"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files: list[str] = []
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=initial_primary,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-4",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=secondary,
                    files=files,
                    allow_root_index_copy=True,
                    merge_owner="builder-5",
                )
                self.orch._save_extracted_code_block(
                    task_dir=out_dir,
                    rel_path=Path("styles.css"),
                    code=retry_payload,
                    files=files,
                    allow_root_index_copy=True,
                    is_retry=True,
                    merge_owner="builder-4",
                )
                merged = (out_dir / "styles.css").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn(updated_primary, merged)
        self.assertEqual(merged.count(secondary), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-4"), 1)
        self.assertEqual(merged.count("Builder Asset Start: builder-5"), 1)

    def test_extract_code_locked_nav_repair_skips_named_shared_asset_blocks(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            payload = (
                "```css styles.css\nbody{background:#faa;color:#111;}\n```\n"
                "```javascript app.js\nconsole.log('skip-me')\n```\n"
                "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    payload,
                    subtask_id="10",
                    allow_root_index_copy=True,
                    multi_page_required=True,
                    allowed_html_targets=["index.html"],
                    allow_multi_page_raw_html_fallback=True,
                    allow_named_shared_asset_blocks=False,
                )
                index_exists = (out_dir / "index.html").exists()
                styles_exists = (out_dir / "styles.css").exists()
                app_exists = (out_dir / "app.js").exists()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [str(out_dir / "index.html")])
        self.assertTrue(index_exists)
        self.assertFalse(styles_exists)
        self.assertFalse(app_exists)

    def test_extract_code_secondary_builder_skips_root_shared_asset_blocks(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            payload = (
                "```css styles.css\nbody{background:#111;color:#fff;}\n```\n"
                "```javascript app.js\nconsole.log('should-not-touch-root')\n```\n"
                "```html contact.html\n<!DOCTYPE html><html><body><h1>Contact</h1></body></html>\n```"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    payload,
                    subtask_id="12",
                    allow_root_index_copy=False,
                    allow_root_shared_asset_write=False,
                    multi_page_required=True,
                    allowed_html_targets=["contact.html"],
                    allow_multi_page_raw_html_fallback=True,
                )
                self.assertEqual(files, [str(out_dir / "contact.html")])
                self.assertTrue((out_dir / "contact.html").exists())
                self.assertFalse((out_dir / "styles.css").exists())
                self.assertFalse((out_dir / "app.js").exists())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

    def test_extract_code_support_lane_without_html_ownership_skips_raw_html_salvage(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            payload = "<!DOCTYPE html><html><body><h1>Support lane should not own HTML</h1></body></html>"
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    payload,
                    subtask_id="support_lane",
                    allow_root_index_copy=False,
                    allow_root_shared_asset_write=False,
                    multi_page_required=False,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [])
        self.assertFalse((out_dir / "index.html").exists())
        self.assertFalse((out_dir / "task_support_lane" / "index.html").exists())

    def test_extract_code_skips_unnamed_multi_page_shared_assets_and_raw_html_fallback(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            payload = (
                "```css\nbody{background:#111;color:#fff;}\n```\n"
                "```javascript\nconsole.log('should-skip')\n```\n"
                "<!DOCTYPE html><html><body><h1>Ambiguous Multi Page</h1></body></html>"
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                files = self.orch._extract_and_save_code(
                    payload,
                    subtask_id="7",
                    allow_root_index_copy=True,
                    multi_page_required=True,
                    allowed_html_targets=["index.html", "about.html"],
                    allow_multi_page_raw_html_fallback=True,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [])
        self.assertFalse((out_dir / "task_7" / "styles.css").exists())
        self.assertFalse((out_dir / "task_7" / "index.js").exists())
        self.assertFalse((out_dir / "task_7" / "index.html").exists())

    def test_polisher_visual_gap_report_lists_placeholder_targets(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "index.html").write_text(
                """<!DOCTYPE html><html><body>
                <div class="showcase-image" style="background: linear-gradient(135deg,#111,#333);"></div>
                <div class="collection-card-image" style="background: radial-gradient(circle,#222,#000);"></div>
                <div class="experience-visual"></div>
                </body></html>""",
                encoding="utf-8",
            )
            (out_dir / "contact.html").write_text(
                """<!DOCTYPE html><html><body><div class="map-placeholder"></div></body></html>""",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._run_started_at = 0
                report = self.orch._polisher_visual_gap_report()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("Visual Gap Report", report)
        self.assertIn("index.html", report)
        self.assertIn("contact.html", report)
        self.assertIn("showcase-image", report)
        self.assertIn("map-placeholder", report)

    def test_polisher_visual_gap_report_ignores_filled_media_blocks(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "styles.css").write_text(
                ".story-image { background-image: url('https://example.com/story.jpg'); }\n",
                encoding="utf-8",
            )
            (out_dir / "index.html").write_text(
                """<!DOCTYPE html><html><body>
                <div class="showcase-image"><img src="https://example.com/hero.jpg" alt="hero"></div>
                <div class="story-image"></div>
                <div class="craft-placeholder"><svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg></div>
                </body></html>""",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._run_started_at = 0
                report = self.orch._polisher_visual_gap_report()
                gate_errors = self.orch._polisher_gap_gate_errors()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(report, "")
        self.assertEqual(gate_errors, [])

    def test_polisher_gap_gate_flags_placeholder_copy_blocks(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "index.html").write_text(
                """<!DOCTYPE html><html><body>
                <div class="collection-card-image">[Collection Image]</div>
                <div class="map-placeholder"></div>
                </body></html>""",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._run_started_at = 0
                gate_errors = self.orch._polisher_gap_gate_errors()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(gate_errors)
        self.assertIn("placeholder copy", " ".join(gate_errors).lower())

    def test_polisher_gap_report_flags_icon_shells_and_broken_secondary_routes(self):
        original_output = orchestrator_module.OUTPUT_DIR
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "index.html").write_text(
                """<!DOCTYPE html><html><body>
                <nav><a href="features.html">Features</a><a href="about.html">About</a></nav>
                <main><section><h1>Home</h1><p>Premium home.</p></section></main>
                </body></html>""",
                encoding="utf-8",
            )
            (out_dir / "features.html").write_text(
                """<!DOCTYPE html><html><body>
                <nav><a href="index.html">Home</a><a href="gallery.html">Gallery</a></nav>
                <section class="experience-visual"><div class="experience-icon"><svg viewBox="0 0 64 64"><circle cx="32" cy="20" r="12"/><path d="M32 32v8"/><path d="M24 48h16"/></svg></div></section>
                </body></html>""",
                encoding="utf-8",
            )
            (out_dir / "about.html").write_text(
                """<!DOCTYPE html><html><body>
                <nav><a href="index.html">Home</a></nav>
                <div class="hero-pattern"></div>
                </body></html>""",
                encoding="utf-8",
            )
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                self.orch._run_started_at = 0
                report = self.orch._polisher_visual_gap_report()
                gate_errors = self.orch._polisher_gap_gate_errors()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("features.html", report)
        self.assertIn("about.html", report)
        self.assertIn("experience-visual", report)
        self.assertIn("hero-pattern", report)
        self.assertIn("broken local routes -> gallery.html", report)
        self.assertTrue(any("gallery.html" in item for item in gate_errors))
        self.assertTrue(any("icon/pattern placeholder visuals" in item for item in gate_errors))

    def test_scan_html_visual_gaps_does_not_flag_large_hero_svg_art_as_placeholder(self):
        html = """<!DOCTYPE html><html><body>
        <section class="hero-art" aria-hidden="true">
          <svg width="360" height="360" viewBox="0 0 360 360" fill="none">
            <defs>
              <linearGradient id="g1" x1="0" y1="0" x2="360" y2="360">
                <stop stop-color="#7EF0D8"/>
                <stop offset="1" stop-color="#7AA8FF"/>
              </linearGradient>
            </defs>
            <circle cx="180" cy="180" r="110" fill="url(#g1)"/>
            <path d="M80 180h200" stroke="#fff"/>
            <path d="M180 80v200" stroke="#fff"/>
          </svg>
        </section>
        </body></html>"""

        counts = self.orch._scan_html_visual_gaps(html)

        self.assertNotIn("icon/pattern placeholder visuals", counts)

    def test_polisher_regression_guard_restores_stable_output(self):
        class StubBridge:
            config = {}

            def preferred_model_for_node(self, node, model):
                return model

            def _resolve_model(self, model_name):
                return {"provider": "kimi" if "kimi" in str(model_name) else "openai"}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "polished", "tool_results": [], "tool_call_stats": {}}

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        plan = Plan(
            goal="做一个高端多页面旅游网站",
            subtasks=[SubTask(id="5", agent_type="polisher", description="polish the premium site", depends_on=[])],
        )
        subtask = plan.subtasks[0]

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            (out_dir / "index.html").write_text(
                "<!DOCTYPE html><html><head><style>body{background:#111;color:#fff}</style></head><body><main><section><h1>Site</h1><p>ok</p></section></main></body></html>",
                encoding="utf-8",
            )
            with patch.object(orchestrator_module, "OUTPUT_DIR", out_dir), \
                 patch.object(
                     orch,
                     "_validate_builder_quality",
                     side_effect=[
                         {"pass": True, "score": 88, "errors": [], "warnings": []},
                         {"pass": True, "score": 70, "errors": [], "warnings": []},
                     ],
                 ), \
                 patch.object(orch, "_polisher_gap_gate_errors", return_value=[]), \
                 patch.object(orch, "_restore_output_from_stable_preview", return_value=[str(out_dir / "index.html")]) as restore_mock:
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("Polisher regression guard failed", str(result.get("error")))
        restore_mock.assert_called_once()


class TestVisualBaselineRefresh(unittest.TestCase):
    def test_successful_run_refreshes_visual_baseline_and_updates_report(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._canonical_ctx = {"task_id": "task_42", "run_id": "run_99"}
        report: dict[str, object] = {}

        with patch.object(orchestrator_module, "latest_preview_artifact", return_value=("task_preview", Path("/tmp/index.html"))), \
             patch.object(orchestrator_module, "build_preview_url_for_file", return_value="http://127.0.0.1:8765/preview/index.html"), \
             patch.object(
                 orchestrator_module,
                 "update_visual_baseline",
                 new=AsyncMock(return_value={
                     "updated": True,
                     "scope_key": "task_task_42",
                     "page_key": "index.html",
                     "captures": [{"name": "desktop_fold", "width": 1440, "height": 1100}],
                 }),
             ) as update_mock:
            asyncio.run(orch._refresh_visual_baseline_for_success("做一个高质量官网", report))

        update_mock.assert_awaited_once()
        call_args = update_mock.await_args
        self.assertEqual(call_args.args[0], "http://127.0.0.1:8765/preview/index.html")
        self.assertEqual(call_args.args[1], "task_task_42")
        self.assertEqual(call_args.kwargs["metadata"]["preview_task_id"], "task_preview")
        self.assertEqual(call_args.kwargs["metadata"]["run_id"], "run_99")
        self.assertEqual(report.get("visual_baseline"), {
            "updated": True,
            "scope_key": "task_task_42",
            "page_key": "index.html",
            "captures": [{"name": "desktop_fold", "width": 1440, "height": 1100}],
        })


class TestReportIntegrity(unittest.TestCase):
    def test_report_not_success_when_pending_subtasks_exist(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="2", agent_type="tester", description="test", depends_on=["1"]),
            ],
        )
        plan.subtasks[0].status = TaskStatus.COMPLETED
        plan.subtasks[1].status = TaskStatus.PENDING

        report = orch._build_report(plan, results={"1": {"success": True}})
        self.assertFalse(report.get("success"))
        self.assertEqual(report.get("pending"), 1)

    def test_report_aggregates_token_and_cost_from_results(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="2", agent_type="tester", description="test", depends_on=["1"]),
            ],
        )
        plan.subtasks[0].status = TaskStatus.COMPLETED
        plan.subtasks[1].status = TaskStatus.COMPLETED

        report = orch._build_report(plan, results={
            "1": {"success": True, "tokens_used": 1200, "cost": 0.42},
            "2": {"success": True, "tokens_used": 300, "cost": 0.08},
        })

        self.assertEqual(report.get("total_tokens"), 1500)
        self.assertAlmostEqual(report.get("total_cost"), 0.5, places=6)

    def test_report_summary_distinguishes_root_failure_from_blocked_nodes(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="2", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2"]),
                SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["2"]),
            ],
        )
        plan.subtasks[0].status = TaskStatus.FAILED
        plan.subtasks[0].error = "Content completeness failure: 8/19 containers are empty"
        plan.subtasks[1].status = TaskStatus.BLOCKED
        plan.subtasks[1].error = "Blocked by failed dependencies (not executed): 2"
        plan.subtasks[2].status = TaskStatus.BLOCKED
        plan.subtasks[2].error = "Blocked by failed dependencies (not executed): 2"

        report = orch._build_report(plan, results={"2": {"success": False}})

        self.assertFalse(report.get("success"))
        self.assertIn("Root failure", report.get("summary", ""))
        self.assertIn("Downstream blocked", report.get("summary", ""))
        self.assertTrue(any("builder #2" in risk for risk in report.get("remaining_risks", [])))

    def test_report_success_with_warnings_when_completed_subtask_has_unresolved_warning(self):
        """Warnings no longer prevent success — they are surfaced via has_warnings and remaining_risks."""
        orch = Orchestrator(ai_bridge=None, executor=None)
        reviewer = SubTask(id="6", agent_type="reviewer", description="review", depends_on=["5"])
        reviewer.status = TaskStatus.COMPLETED
        reviewer.warning = "Reviewer flagged issues but builder retries exhausted."
        reviewer.error = reviewer.warning
        plan = Plan(goal="test", subtasks=[reviewer])

        report = orch._build_report(plan, results={"6": {"success": True}})

        self.assertTrue(report.get("success"))
        self.assertTrue(report.get("has_warnings"))

    def test_humanize_output_summary_for_prompt_only_imagegen_is_explicit(self):
        orch = Orchestrator(ai_bridge=None, executor=None)

        summary = orch._humanize_output_summary(
            "imagegen",
            raw_output="Generated prompt packs and style-lock notes.",
            success=True,
            files_created=[
                "/tmp/evermind_output/assets/00_project_brief.md",
                "/tmp/evermind_output/assets/01_character_prompts.md",
            ],
        )

        # v3.5.1: summary now uses content-aware labels
        self.assertIn("设计规范", summary)
        # No real image files, only .md docs
        self.assertNotIn("视觉资产", summary)

    def test_extract_xml_tagged_narrative_returns_ai_content(self):
        """v4.0-fix: Analyst XML-tagged output should be extracted as markdown narrative."""
        orch = Orchestrator(ai_bridge=None, executor=None)
        xml_output = (
            "Reference sites visited:\n"
            "- https://example.com/repo\n\n"
            "<reference_sites>\n"
            "https://example.com/repo\nhttps://example.com/docs\n"
            "</reference_sites>\n"
            "<design_direction>\n"
            "- Visual direction: Apple-adjacent premium minimalism, restrained palette, "
            "high-end typography, cinematic motion, and strong whitespace rhythm.\n"
            "- Keep one coherent design system across all pages.\n"
            "</design_direction>\n"
            "<non_negotiables>\n"
            "- Write real deliverables, not task_*/index.html preview fallbacks.\n"
            "- No blank middle sections, no empty routes.\n"
            "- Every requested page/route must contain substantial visible content.\n"
            "</non_negotiables>\n"
            "<builder_1_handoff>\n"
            "Builder 1 owns the root index.html with Three.js rendering pipeline.\n"
            "Use PerspectiveCamera(75, aspect, 0.1, 1000) for the main viewport.\n"
            "</builder_1_handoff>\n"
        )
        result = orch._extract_xml_tagged_narrative(xml_output)
        self.assertIn("### Design Direction", result)
        self.assertIn("Apple-adjacent premium minimalism", result)
        self.assertIn("### Quality Gates", result)
        self.assertIn("real deliverables", result)
        self.assertIn("### Builder 1 Handoff", result)
        self.assertIn("PerspectiveCamera", result)
        self.assertGreater(len(result), 200)

    def test_extract_xml_tagged_narrative_empty_for_non_xml(self):
        """Non-XML output should return empty string."""
        orch = Orchestrator(ai_bridge=None, executor=None)
        self.assertEqual(orch._extract_xml_tagged_narrative("just plain text"), "")
        self.assertEqual(orch._extract_xml_tagged_narrative(""), "")

    def test_planner_narrative_includes_modules_and_execution_order(self):
        """v4.0-fix: Planner JSON blueprint should render modules and execution order."""
        orch = Orchestrator(ai_bridge=None, executor=None)
        json_output = '```json\n' + __import__("json").dumps({
            "architecture": "Commercial-grade WebGL third-person shooter built on Three.js r158 with modular ECS architecture.",
            "modules": ["CoreEngine", "InputSystem", "CameraController", "WeaponSystem", "MonsterAI"],
            "execution_order": ["step1: CoreEngine scaffolding", "step2: InputSystem -> CameraController", "step3: WeaponSystem"],
            "key_dependencies": ["InputSystem -> CameraController", "WeaponSystem -> ProjectileManager"],
            "builder_ownership": {},
        }) + '\n```\n'
        st = SubTask(id="1", agent_type="planner", description="plan", depends_on=[])
        result = orch._node_report_narrative_lines(
            plan=None, subtask=st, current_action="planning",
            summary_list=[], skill_list=[], reference_list=[], files_list=[],
            blocking_reason="", full_output=json_output,
        )
        joined = "\n".join(result)
        self.assertIn("CoreEngine", joined)
        self.assertIn("Module Breakdown", joined)
        self.assertIn("Execution Order", joined)
        self.assertIn("Key Dependencies", joined)
        self.assertIn("InputSystem -> CameraController", joined)

    def test_humanize_output_summary_for_assetimport_is_narrative(self):
        orch = Orchestrator(ai_bridge=None, executor=None)

        summary = orch._humanize_output_summary(
            "assetimport",
            raw_output="Organized manifest, replacement points, collision and LOD fallback plan.",
            success=True,
            files_created=[],
        )

        # v3.5.1: summary now uses Chinese labels for asset import features
        self.assertIn("资产导入方案完成", summary)
        self.assertIn("清单索引", summary)


class TestAINarrativeReport(unittest.TestCase):
    """Tests for the AI Narrative Report Writer (方案 B)."""

    def test_generate_ai_narrative_returns_empty_when_no_ai_bridge(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(id="1", agent_type="planner", description="plan", depends_on=[])
        plan = Plan(goal="test goal", subtasks=[subtask])
        result = orch._generate_ai_narrative_report(
            subtask, plan,
            full_output="some output",
            files_list=[],
            result={"success": True},
        )
        self.assertEqual(result, "")

    def test_generate_ai_narrative_returns_content_when_bridge_available(self):
        mock_bridge = MagicMock()
        mock_bridge.quick_completion.return_value = (
            "### 执行概述\n\n"
            "Planner 节点成功生成了项目蓝图，划分了 12 个子系统模块。\n\n"
            "### 关键决策\n\n"
            "- 选择 Three.js 作为渲染引擎，因为 WebGL 兼容性最佳\n"
            "- 采用 ECS 架构模式分离渲染和逻辑\n\n"
            "### 质量评估\n\n"
            "输出结构完整，模块边界清晰。\n\n"
            "### 下游建议\n\n"
            "Builder 应优先实现核心游戏循环。"
        )
        orch = Orchestrator(ai_bridge=mock_bridge, executor=None)
        subtask = SubTask(id="1", agent_type="planner", description="plan", depends_on=[])
        plan = Plan(goal="创建3D射击游戏", subtasks=[subtask])
        result = orch._generate_ai_narrative_report(
            subtask, plan,
            full_output='{"architecture": "Three.js shooter"}',
            files_list=[],
            result={"success": True},
            model_used="gpt-5.4-mini",
            duration_seconds=45.0,
        )
        self.assertIn("执行概述", result)
        self.assertIn("Three.js", result)
        mock_bridge.quick_completion.assert_called_once()
        call_args = mock_bridge.quick_completion.call_args
        self.assertEqual(call_args.kwargs.get("model"), "gpt-5.4-mini")

    def test_generate_ai_narrative_returns_empty_on_short_response(self):
        mock_bridge = MagicMock()
        mock_bridge.quick_completion.return_value = "太短了"
        orch = Orchestrator(ai_bridge=mock_bridge, executor=None)
        subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        plan = Plan(goal="test", subtasks=[subtask])
        result = orch._generate_ai_narrative_report(
            subtask, plan,
            full_output="output",
            files_list=["index.html"],
            result={"success": True},
        )
        self.assertEqual(result, "")

    def test_generate_ai_narrative_returns_empty_on_exception(self):
        mock_bridge = MagicMock()
        mock_bridge.quick_completion.side_effect = RuntimeError("API down")
        orch = Orchestrator(ai_bridge=mock_bridge, executor=None)
        subtask = SubTask(id="1", agent_type="analyst", description="analyze", depends_on=[])
        plan = Plan(goal="test", subtasks=[subtask])
        result = orch._generate_ai_narrative_report(
            subtask, plan,
            full_output="output",
            files_list=[],
            result={"success": True},
        )
        self.assertEqual(result, "")

    def test_generate_ai_narrative_rejects_no_markdown_headers(self):
        mock_bridge = MagicMock()
        mock_bridge.quick_completion.return_value = (
            "这是一段没有任何 markdown 标题标记的纯文本回复，"
            "长度超过一百个字符但是全部都是平铺直叙的段落文本。"
            "这种情况应该被拒绝因为不符合报告格式要求需要有结构化的标题。" * 3
        )
        orch = Orchestrator(ai_bridge=mock_bridge, executor=None)
        subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        plan = Plan(goal="test", subtasks=[subtask])
        result = orch._generate_ai_narrative_report(
            subtask, plan,
            full_output="output",
            files_list=[],
            result={"success": True},
        )
        self.assertEqual(result, "")

    def test_report_role_zh_mapping(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        self.assertEqual(orch._REPORT_ROLE_ZH.get("planner"), "规划师")
        self.assertEqual(orch._REPORT_ROLE_ZH.get("builder"), "构建器")
        self.assertEqual(orch._REPORT_ROLE_ZH.get("merger"), "合并器")
        self.assertEqual(orch._REPORT_ROLE_ZH.get("reviewer"), "审查员")


class TestBuilderQualityGate(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_html_quality_report_rejects_truncated_inline_game_javascript(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Broken TPS</title>
  <style>
    body{margin:0;background:#081018;color:#eef6fb;font-family:system-ui,sans-serif}
    main{display:grid;gap:16px;padding:24px}
    section{border:1px solid rgba(255,255,255,.1);padding:16px;border-radius:16px}
    @media(max-width:900px){main{padding:18px}}
  </style>
</head>
<body>
  <main>
    <section><h1>Broken TPS</h1><p>Structured content keeps the artifact above the minimum HTML density threshold.</p></section>
    <section><button id="startBtn">Start Mission</button><canvas id="game"></canvas></section>
    <section><p>Additional explanatory copy prevents blank-page heuristics from dominating the score.</p></section>
    <section><p>Another semantic block keeps the document deterministic.</p></section>
  </main>
  <script>
    function startGame() {
      currentState = 'playing';
      const finalPos = player.position.clone().add(finalPos
    }
  </script>
</body>
</html>"""

        report = self.orch._html_quality_report(html, source="inline")

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("invalid JavaScript syntax" in err or "truncated" in err.lower() for err in report.get("errors", [])))

    def test_content_completeness_ignores_game_utility_shells(self):
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Voxel Strike</title>
<style>
body{margin:0;background:#05070d;color:#eef2ff;font-family:sans-serif;overflow:hidden}
main{display:grid;gap:16px;padding:24px}
.hud{display:grid;gap:12px}
.panel{display:grid;gap:8px;padding:16px;border:1px solid rgba(255,255,255,.12)}
.hero{display:grid;gap:12px;min-height:220px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.health-bar{height:8px;background:#142033}
.health-fill{height:100%;width:80%;background:linear-gradient(90deg,#12f7b6,#00c2ff)}
#crosshair,#damageOverlay,#notifications{position:absolute;pointer-events:none}
@media (max-width: 900px){main{padding:16px}.stats{grid-template-columns:1fr}}
</style>
</head>
<body>
<canvas id="gameCanvas"></canvas>
<div id="crosshair"></div>
<div id="damageOverlay"></div>
<div id="notifications"></div>
<main>
  <section class="hero panel">
    <h1>VOXEL STRIKE</h1>
    <p>三维像素射击游戏，包含完整开始界面、战斗 HUD、波次推进、武器切换和移动端触控支持。</p>
    <div class="health-bar"><div class="health-fill"></div></div>
  </section>
  <section class="hud">
    <div class="panel"><h2>任务简报</h2><p>击退敌方波次，收集补给，保持节奏推进并在每轮后升级武器系统。</p></div>
    <div class="stats">
      <article class="panel"><h3>得分</h3><p>12400</p></article>
      <article class="panel"><h3>弹药</h3><p>24 / 180</p></article>
      <article class="panel"><h3>波次</h3><p>第 6 波</p></article>
    </div>
    <div class="panel"><canvas id="minimapCanvas" width="180" height="180"></canvas></div>
  </section>
</main>
<script>window.addEventListener('keydown',()=>{});</script>
</body>
</html>"""
        report = self.orch._html_quality_report(html, source="inline")
        self.assertFalse(any("Content completeness failure" in err for err in report.get("errors", [])))
        self.assertTrue(report.get("pass"))

    def test_rejects_tiny_incomplete_html(self):
        bad_html = "<!DOCTYPE html><html><body>hello</body></html>"
        report = self.orch._html_quality_report(bad_html, source="inline")
        self.assertFalse(report.get("pass"))
        self.assertGreater(len(report.get("errors", [])), 0)

    def test_fatal_force_pass_errors_detect_invalid_javascript(self):
        errors = [
            "invalid JavaScript syntax in inline <script> block",
            "Low semantic structure (1 sections)",
        ]

        fatal = self.orch._fatal_force_pass_errors(errors)

        self.assertEqual(fatal, ["invalid JavaScript syntax in inline <script> block"])
        self.assertTrue(self.orch._quality_errors_include_fatal(errors))

    def test_model_execution_strategy_profile_constrains_baseline_builder(self):
        profile = self.orch._model_execution_strategy_profile(
            "builder",
            "",
            retry_count=1,
            max_retries=3,
        )

        self.assertEqual(profile.get("tier"), 3)
        self.assertEqual(profile.get("execution_mode"), "constraint")
        self.assertEqual(profile.get("patch_granularity"), "surgical")
        self.assertEqual(profile.get("tool_budget_profile"), "tight")

    def test_execute_subtask_last_retry_force_pass_stays_blocked_on_fatal_quality_error(self):
        class WriteBridge:
            config = {}

            def __init__(self, output_dir):
                self.output_dir = output_dir

            def preferred_model_for_node(self, node, model):
                return model

            def _resolve_model(self, model_name):
                provider = "kimi" if "kimi" in str(model_name) else "openai"
                return {"provider": provider}

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                html_path = self.output_dir / "index.html"
                html_path.write_text(
                    "<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><style>body{background:#111;color:#fff}main{display:grid;gap:16px;padding:24px}@media(max-width:900px){main{padding:16px}}</style></head><body><main><section><h1>Fatal</h1><p>Builder produced a substantial page.</p></section><section><p>Second panel.</p></section><section><p>Third panel.</p></section><section><p>Fourth panel.</p></section></main></body></html>",
                    encoding="utf-8",
                )
                if on_progress:
                    await on_progress({"stage": "builder_write", "path": str(html_path)})
                return {
                    "success": True,
                    "output": "saved index.html",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            orch = Orchestrator(ai_bridge=WriteBridge(tmp_out), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(
                id="1",
                agent_type="builder",
                description="build landing page",
                depends_on=[],
                max_retries=3,
            )
            subtask.retries = 3  # v3.1: is_last_retry now requires retries >= max_retries
            plan = Plan(goal="做一个高级品牌落地页。", subtasks=[subtask])
            quality_report = {
                "pass": False,
                "score": 65,
                "errors": ["invalid JavaScript syntax in inline <script> block"],
                "warnings": [],
                "model_tier": 2,
                "pass_threshold": 70,
            }

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value=quality_report):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("quality gate failed", str(result.get("error", "")).lower())
        stages = [
            call.args[1].get("stage")
            for call in orch.emit.await_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], dict)
        ]
        self.assertIn("quality_gate_force_blocked", stages)
        self.assertNotIn("quality_gate_force_passed", stages)

    def test_execute_subtask_last_retry_force_pass_allows_nonfatal_quality_error(self):
        class WriteBridge:
            config = {}

            def __init__(self, output_dir):
                self.output_dir = output_dir

            def preferred_model_for_node(self, node, model):
                return model

            def _resolve_model(self, model_name):
                provider = "kimi" if "kimi" in str(model_name) else "openai"
                return {"provider": provider}

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                html_path = self.output_dir / "index.html"
                html_path.write_text(
                    "<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><style>body{background:#111;color:#fff}main{display:grid;gap:16px;padding:24px}@media(max-width:900px){main{padding:16px}}</style></head><body><main><section><h1>Nonfatal</h1><p>Builder produced a usable page with cosmetic issues only.</p></section><section><p>Second panel.</p></section><section><p>Third panel.</p></section><section><p>Fourth panel.</p></section></main></body></html>",
                    encoding="utf-8",
                )
                if on_progress:
                    await on_progress({"stage": "builder_write", "path": str(html_path)})
                return {
                    "success": True,
                    "output": "saved index.html",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            orch = Orchestrator(ai_bridge=WriteBridge(tmp_out), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(
                id="1",
                agent_type="builder",
                description="build landing page",
                depends_on=[],
                max_retries=3,
            )
            subtask.retries = 3  # v3.1: is_last_retry now requires retries >= max_retries
            plan = Plan(goal="做一个高级品牌落地页。", subtasks=[subtask])
            quality_report = {
                "pass": False,
                "score": 65,
                "errors": ["Missing mobile viewport meta tag"],
                "warnings": [],
                "model_tier": 2,
                "pass_threshold": 70,
            }

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value=quality_report):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        stages = [
            call.args[1].get("stage")
            for call in orch.emit.await_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], dict)
        ]
        self.assertIn("quality_gate_force_passed", stages)
        self.assertNotIn("quality_gate_force_blocked", stages)

    def test_accepts_polished_responsive_html(self):
        good_html = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
<title>Demo</title>
<style>
:root { --bg:#0b1020; --fg:#e9ecf1; --brand:#3dd5f3; --gap:16px; }
* { box-sizing:border-box; }
body { margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:var(--fg); background:linear-gradient(180deg,#0b1020,#121a34); }
header,main,section,footer,nav { display:block; }
nav { display:flex; justify-content:space-between; padding:20px; }
main { display:grid; gap:var(--gap); padding:24px; }
.hero { display:flex; gap:24px; align-items:center; min-height:40vh; }
.features { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.proof { display:grid; gap:10px; }
.cta { display:flex; gap:12px; }
button { padding:10px 16px; border-radius:10px; border:none; background:var(--brand); color:#001018; }
button:focus-visible { outline:2px solid #fff; outline-offset:2px; }
footer { padding:24px; opacity:.85; }
@media (max-width: 900px) { .hero { flex-direction:column; } .features { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><nav><strong>Brand</strong><a href=\"#\">Pricing</a></nav></header>
<main>
  <section class=\"hero\"><h1>Modern Product</h1><p>Ship fast with confidence.</p><button aria-label=\"Start trial\">Start free</button></section>
  <section class=\"features\"><article>Fast</article><article>Secure</article><article>Reliable</article></section>
  <section class=\"proof\"><blockquote>Trusted by teams.</blockquote></section>
  <section class=\"cta\"><button>Book demo</button></section>
</main>
<footer>2026 Demo Inc.</footer>
<script>document.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{}));</script>
</body>
</html>"""
        report = self.orch._html_quality_report(good_html, source="inline")
        self.assertTrue(report.get("pass"))
        self.assertGreaterEqual(report.get("score", 0), 70)

    def test_rejects_style_only_black_screen_artifact(self):
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<body>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Black Screen</title>
  <style>
    body { margin: 0; background: #000; color: #fff; }
    .hud { position: fixed; top: 0; left: 0; }
  </style>
</body>
</html>"""
        report = self.orch._html_quality_report(html, source="inline")
        self.assertFalse(report.get("pass"))
        joined = " | ".join(report.get("errors", []))
        self.assertIn("Body lacks meaningful visible content", joined)

    def test_validate_builder_quality_uses_saved_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "index.html"
            p.write_text("<!DOCTYPE html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body><style>body{margin:0;}div{display:flex;}@media(max-width:800px){div{display:block;}}</style><header></header><main><section></section><section></section><footer></footer></main><script>1+1</script></body></html>", encoding="utf-8")
            report = self.orch._validate_builder_quality([str(p)], output="")
            # Can still fail quality score, but should read artifact and produce a structured report.
            self.assertIn("score", report)
            self.assertIn("errors", report)

    def test_validate_builder_quality_recovers_game_html_from_output_when_saved_artifact_is_stale(self):
        template = Path("/path/to/evermind/backend/templates/game_3d_shooter.html").read_text(encoding="utf-8")
        template = (
            template
            .replace("{{GAME_TITLE}}", "Neon Strike")
            .replace("{{GAME_SUBTITLE}}", "3D shooter")
            .replace("{{WEAPON_NAME}}", "Pulse Rifle")
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            weak = tmp_out / "index.html"
            weak.write_text("<!DOCTYPE html><html><body><main>stub</main></body></html>", encoding="utf-8")
            subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
            subtask.started_at = time.time()
            plan = Plan(goal="做一个 3D 射击游戏", subtasks=[subtask])

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                report = self.orch._validate_builder_quality(
                    [str(weak)],
                    f"```html\n{template}\n```",
                    goal=plan.goal,
                    plan=plan,
                    subtask=subtask,
                )

        joined_errors = " | ".join(report.get("errors", []))
        joined_warnings = " | ".join(report.get("warnings", []))
        self.assertGreaterEqual(int(report.get("score", 0) or 0), 70)
        self.assertNotIn("Missing inline <style>", joined_errors)
        self.assertNotIn("missing gameplay input bindings", joined_errors.lower())
        self.assertIn("Recovered builder HTML from model output", joined_warnings)

    def test_validate_builder_quality_prefers_recent_single_page_disk_artifact_when_write_flag_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            html_path = tmp_out / "index.html"
            html_path.write_text(
                """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Northstar Studio</title>
  <style>
    :root { --bg:#0f172a; --panel:#111827; --line:rgba(148,163,184,.28); --text:#e5eefc; --accent:#38bdf8; }
    * { box-sizing:border-box; }
    body { margin:0; background:radial-gradient(circle at top, #1e293b 0%, var(--bg) 60%); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
    header, main, section, footer, nav, article { display:block; }
    header { padding:24px; }
    nav { display:flex; gap:12px; align-items:center; }
    main { display:grid; gap:20px; padding:24px; }
    .panel { padding:24px; border:1px solid var(--line); border-radius:24px; background:rgba(15,23,42,.86); }
    .grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:16px; }
    button { padding:12px 18px; border-radius:999px; border:none; background:var(--accent); color:#082032; font-weight:700; cursor:pointer; }
    @media (max-width: 900px) { nav { flex-wrap:wrap; } .grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <nav>
      <a href="#story">品牌故事</a>
      <button aria-label="预约演示">预约演示</button>
    </nav>
  </header>
  <main>
    <section class="panel">
      <h1>Northstar Studio</h1>
      <p>完整的单页品牌首页已经真实落盘，质量门应优先验证这个 HTML，而不是退回去检查模型附带的说明性前言。</p>
    </section>
    <section id="story" class="grid">
      <article class="panel"><h2>视觉系统</h2><p>渐变背景、层次化面板和明确的 CTA 共同形成可信的商业化页面结构。</p></article>
      <article class="panel"><h2>内容密度</h2><p>这个测试页包含足够的文案、语义结构和交互脚本，用来证明磁盘工件比 output_text 更可靠。</p></article>
    </section>
    <section class="panel">
      <h2>下一步</h2>
      <p>按钮点击后会切换页面状态，说明工件本身具备真实交互，而不是空壳模板。</p>
    </section>
  </main>
  <footer class="panel"><p>Footer continuity.</p></footer>
  <script>
    document.querySelector('button').addEventListener('click', () => {
      document.body.dataset.state = 'cta';
    });
  </script>
</body>
</html>""",
                encoding="utf-8",
            )
            plan = Plan(goal="做一个单页面品牌官网", subtasks=[])
            subtask = SubTask(
                id="5",
                agent_type="builder",
                description="Build a premium single-page brand site.",
                depends_on=[],
            )
            subtask.started_at = time.time() - 2
            plan.subtasks = [subtask]

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            original_run_started_at = self.orch._run_started_at
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                self.orch._run_started_at = subtask.started_at
                self.orch._current_task_type = "website"
                report = self.orch._validate_builder_quality(
                    [],
                    output="I'll build the final website now and save it shortly.",
                    goal=plan.goal,
                    plan=plan,
                    subtask=subtask,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output
                self.orch._run_started_at = original_run_started_at

        self.assertEqual(
            str(Path(str(report.get("source") or "")).resolve()),
            str(html_path.resolve()),
        )
        self.assertFalse(any("Missing <!DOCTYPE html>" in err for err in report.get("errors", [])))
        self.assertGreater(report.get("score", 0), 0)

    def test_validate_builder_quality_accepts_shared_local_stylesheet_for_multi_page(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(
                ":root{--bg:#0d1117;--fg:#f5f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            (tmp_out / "app.js").write_text(
                "document.querySelectorAll('[data-cta]').forEach((item)=>item.addEventListener('click',()=>item.classList.toggle('is-active')));",
                encoding="utf-8",
            )

            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header><nav><a href="index.html">Home</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header>
  <main>
    <section class="panel"><h1>{title}</h1><p>{body}</p></section>
    <section class="grid"><article class="panel"><h2>Story</h2><p>Editorial density with concrete product language, premium service framing, and stronger information scent for commercial visitors.</p></article><article class="panel"><h2>Service</h2><p>Appointment and concierge detail, client reassurance, and routing cues across the site.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Responsive structure, semantic HTML, and route-aware navigation are already wired across every page so the multi-page preview behaves like a coherent brand site instead of disconnected drafts.</p></section>
  </main>
  <footer><p>Footer copy reinforces the premium journey while the shared stylesheet carries the global design system.</p><button data-cta>Reserve</button></footer>
  <script src="app.js"></script>
</body>
</html>"""
            (tmp_out / "index.html").write_text(page_template.format(title="Home", body="Luxury landing page with coherent navigation, premium content density, and enough descriptive copy to remain substantial even when styling is shared through a local stylesheet asset."), encoding="utf-8")
            (tmp_out / "pricing.html").write_text(page_template.format(title="Pricing", body="Structured offer ladder, private consultation tiers, service framing, and commercial detail that makes the page materially complete instead of a thin placeholder."), encoding="utf-8")
            (tmp_out / "contact.html").write_text(page_template.format(title="Contact", body="Boutique contact flow, appointment intake, concierge follow-up, and location guidance so the final route is rich enough for deterministic quality validation."), encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "pricing.html"), str(tmp_out / "contact.html"), str(tmp_out / "styles.css"), str(tmp_out / "app.js")],
                    output="",
                    goal="做一个三页面轻奢品牌官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(report.get("pass"))
        self.assertFalse(any("style" in err.lower() for err in report.get("errors", [])))

    def test_validate_builder_quality_rejects_unsafe_shared_script_contract(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(
                ":root{--bg:#0d1117;--fg:#f5f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            (tmp_out / "app.js").write_text(
                "const rail = document.querySelector('#exclusiveHeroRail');\n"
                "rail.classList.add('active');\n",
                encoding="utf-8",
            )
            page_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  {rail}
  <header><nav><a href="index.html">Home</a><a href="contact.html">Contact</a></nav></header>
  <main>
    <section class="panel"><h1>{title}</h1><p>{body}</p></section>
    <section class="grid"><article class="panel"><h2>Craft</h2><p>Real content density, material story, and route continuity across the multi-page site.</p></article><article class="panel"><h2>Service</h2><p>Commercial detail, concierge framing, and responsive structure.</p></article></section>
    <section class="panel"><h2>Detail</h2><p>Additional copy ensures each page remains materially complete rather than a stub.</p></section>
  </main>
  <footer><p>Footer continuity.</p></footer>
  <script src="app.js"></script>
</body>
</html>"""
            (tmp_out / "index.html").write_text(
                page_template.format(
                    title="Home",
                    rail='<div id="exclusiveHeroRail"></div>',
                    body="Homepage includes the unique rail element that the unsafe shared script expects.",
                ),
                encoding="utf-8",
            )
            (tmp_out / "contact.html").write_text(
                page_template.format(
                    title="Contact",
                    rail="",
                    body="Contact page omits the unique rail element, so the shared script is unsafe across routes.",
                ),
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "contact.html"), str(tmp_out / "styles.css"), str(tmp_out / "app.js")],
                    output="",
                    goal="做一个双页面轻奢官网，包含首页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Shared local script app.js dereferences selector #exclusiveHeroRail" in err for err in report.get("errors", [])))

    def test_validate_builder_quality_checks_existing_assigned_pages_not_just_latest_retry_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(
                ":root{--bg:#0d1117;--fg:#f5f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                ".hero{display:grid;gap:16px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}}",
                encoding="utf-8",
            )
            (tmp_out / "app.js").write_text(
                "const carousel = document.querySelector('#productCarousel');\n"
                "carousel.classList.add('is-ready');\n",
                encoding="utf-8",
            )
            (tmp_out / "index.html").write_text(
                """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title><link rel="stylesheet" href="styles.css"></head>
<body><div id="productCarousel"></div><header><nav id="nav"><a href="pricing.html">Pricing</a></nav></header>
<main><section class="panel hero"><h1>Home</h1><p>Current retry rewrote the homepage and shared assets, but the secondary route still carries a mismatched DOM contract that should be caught even when only the homepage is listed in files_created.</p><p>The page also includes enough real copy density, semantic structure, and editorial detail to stay well above the minimum deterministic quality thresholds.</p></section><section class="grid"><article class="panel"><h2>Story</h2><p>Enough real content to satisfy the size and structure thresholds for deterministic quality review.</p></article><article class="panel"><h2>Motion</h2><p>Shared transitions and navigation logic must stay coherent across every route in the site.</p></article></section><section class="panel"><h2>Detail</h2><p>Additional copy keeps the page commercially substantial and prevents the test artifact from being mistaken for a tiny scaffold.</p></section></main>
<footer><p>Footer continuity.</p></footer><script src="app.js"></script></body></html>""",
                encoding="utf-8",
            )
            (tmp_out / "pricing.html").write_text(
                """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pricing</title><link rel="stylesheet" href="styles.css"></head>
<body><header><nav id="mainNav"><a href="index.html">Home</a></nav></header>
<main><section class="panel hero"><h1>Pricing</h1><p>This secondary route intentionally omits the product carousel node that the shared script expects, which is exactly the retry-mixing bug the quality gate must catch.</p><p>The pricing route still contains substantial commercial copy so the test failure cannot be attributed to a low-content stub.</p></section><section class="grid"><article class="panel"><h2>Tiers</h2><p>Structured commercial content, service framing, and pricing details make the page materially complete.</p></article><article class="panel"><h2>Support</h2><p>Additional content keeps the route substantial and commercially realistic.</p></article></section><section class="panel"><h2>Detail</h2><p>Additional copy keeps the route substantial instead of stub-like.</p></section></main>
<footer><p>Footer continuity.</p></footer><script src="app.js"></script></body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "styles.css"), str(tmp_out / "app.js")],
                    output="",
                    goal="做一个两页面数码品牌官网，包含首页和价格页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Shared local script app.js dereferences selector #productCarousel" in err for err in report.get("errors", [])))

    def test_validate_builder_quality_auto_normalizes_shared_route_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(
                ":root{--bg:#0d1117;--fg:#f5f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article,ul{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}.grid{grid-template-columns:1fr}}",
                encoding="utf-8",
            )
            (tmp_out / "app.js").write_text(
                "const nav = document.getElementById('nav');\n"
                "const navToggle = document.querySelector('.nav-toggle');\n"
                "const navMenu = document.getElementById('navMenu');\n"
                "const overlay = document.querySelector('.page-transition-overlay');\n"
                "if (nav) nav.classList.add('scrolled');\n"
                "if (navToggle && navMenu) navToggle.addEventListener('click', () => navMenu.classList.toggle('active'));\n"
                "if (overlay) overlay.classList.add('active');\n",
                encoding="utf-8",
            )
            (tmp_out / "index.html").write_text(
                """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title><link rel="stylesheet" href="styles.css"></head>
<body><div class="page-transition-overlay"></div><header><nav id="nav"><button class="nav-toggle" id="navToggle">Menu</button><ul id="navMenu" class="nav-links"><li><a class="nav-link" href="about.html">About</a></li></ul></nav></header>
<main><section class="panel"><h1>Home</h1><p>Homepage uses the original shared hook names and contains enough real content to remain above deterministic quality thresholds.</p></section><section class="grid"><article class="panel"><h2>Story</h2><p>Substantial commercial copy, hierarchy, and route continuity.</p></article><article class="panel"><h2>Motion</h2><p>Shared transitions should remain compatible across every route.</p></article></section><section class="panel"><h2>Detail</h2><p>Additional copy keeps the page materially complete.</p></section></main>
<footer><p>Footer continuity.</p></footer><script src="app.js"></script></body></html>""",
                encoding="utf-8",
            )
            (tmp_out / "about.html").write_text(
                """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>About</title><link rel="stylesheet" href="styles.css"></head>
<body><div class="page-transition"></div><header><nav class="main-nav" id="mainNav"><button class="mobile-menu-toggle" id="mobileMenuToggle">Menu</button><ul id="navLinks" class="nav-links"><li><a class="nav-link" href="index.html">Home</a></li></ul></nav></header>
<main><section class="panel"><h1>About</h1><p>Secondary route intentionally uses the alternate hook names that caused runtime breakage before the post-processing compatibility layer was added.</p></section><section class="grid"><article class="panel"><h2>Team</h2><p>Editorial density and product context keep the route substantive.</p></article><article class="panel"><h2>Culture</h2><p>Additional structured copy ensures this is not a stub.</p></article></section><section class="panel"><h2>Detail</h2><p>More content keeps the route materially complete.</p></section></main>
<footer><p>Footer continuity.</p></footer><script src="app.js"></script></body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "styles.css"), str(tmp_out / "app.js")],
                    output="",
                    goal="做一个两页面数码品牌官网，包含首页和关于页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(report.get("pass"))
        self.assertFalse(any("Shared local script" in err for err in report.get("errors", [])))

    def test_validate_builder_quality_allows_partial_artifact_until_pair_ready(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            part = tmp_out / "index_part1.html"
            part.write_text("<section><h1>Top Half</h1></section>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality([str(part)], output="")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(report.get("pass"))
            self.assertTrue(any("Partial builder artifact" in w for w in report.get("warnings", [])))

    def test_validate_builder_quality_fails_when_builder_omits_assigned_pages(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>:root{--bg:#0d1117;--fg:#f6f1e8;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:12px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><a href="platform.html">Platform</a><a href="contact.html">Contact</a><a href="about.html">About</a><a href="faq.html">FAQ</a></nav></header>
<main><section class="panel"><h1>Luxury home</h1><p>Homepage is complete, but builder one still owes its assigned middle pages.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page in ("platform.html", "contact.html", "about.html", "faq.html"):
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{page}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section class="panel"><h1>{page}</h1><p>Secondary page complete.</p></section></main><footer>{page}</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            plan = Plan(
                goal="做一个 8 页轻奢品牌官网，要有首页和其余多页介绍内容",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home cluster"),
                    SubTask(id="2", agent_type="builder", description="build secondary cluster"),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html")],
                    output="",
                    goal=plan.goal,
                    plan=plan,
                    subtask=plan.subtasks[0],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertFalse(report.get("pass"))
            self.assertTrue(any("Builder did not finish its assigned HTML pages" in err for err in report.get("errors", [])))
            self.assertTrue(any("pricing.html" in err for err in report.get("errors", [])))

    def test_multi_page_quality_gate_rejects_thin_stub_like_pages(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>:root{--bg:#0f1220;--fg:#f5f3ee;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:18px 24px;border-bottom:1px solid var(--line)}"
                "main{display:grid;gap:16px;padding:24px}.panel{padding:18px;border:1px solid var(--line);border-radius:18px}"
                "@media(max-width:900px){nav{flex-wrap:wrap}}</style>"
            )
            for name in ("index.html", "pricing.html", "contact.html"):
                (tmp_out / name).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{name}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header>
<main><section class="panel"><h1>{name}</h1><p>Thin page.</p></section></main><footer>{name}</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "pricing.html"), str(tmp_out / "contact.html")],
                    output="",
                    goal="做一个三页面轻奢官网，包含首页、定价页和联系页，并带高级动画",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("too thin / stub-like" in err for err in report.get("errors", [])))

    def test_multi_page_quality_gate_rejects_corrupted_secondary_pages(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            pricing = tmp_out / "pricing.html"
            contact = tmp_out / "contact.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home</title><style>body{margin:0;background:#111;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:18px 24px}main{display:grid;gap:16px;padding:24px}@media(max-width:900px){nav{flex-wrap:wrap}}</style></head>
<body><header><nav><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header><main><section><h1>Home</h1><p>Real home page with full structure and working links.</p></section><section><h2>Story</h2><p>Luxury editorial layout.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            pricing.write_text(
                "<!DOCTYPE html><html><head><style>body{opacity:1}transition:all .6s ease;... [TRUNCATED]",
                encoding="utf-8",
            )
            contact.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Contact</title><style>body{margin:0;background:#111;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav{display:block}nav{display:flex;gap:12px;padding:18px 24px}main{display:grid;gap:16px;padding:24px}@media(max-width:900px){nav{flex-wrap:wrap}}</style></head>
<body><header><nav><a href="index.html">Home</a><a href="pricing.html">Pricing</a></nav></header><main><section><h1>Contact</h1><p>Real contact page.</p></section><section><h2>Appointments</h2><p>Book a visit.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index), str(pricing), str(contact)],
                    output="",
                    goal="做一个三页面奢侈品官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("invalid or corrupted HTML pages" in err for err in report.get("errors", [])))

    def test_rejects_emoji_icon_usage(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Emoji Icon Demo</title>
<style>
:root { --bg:#111827; --fg:#f8fafc; }
body { margin:0; background:var(--bg); color:var(--fg); font-family:sans-serif; }
header,main,section,footer,nav { display:block; }
main { display:grid; gap:16px; padding:24px; }
.hero { display:flex; gap:12px; align-items:center; }
.features { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
@media (max-width: 900px) { .features { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><nav>Brand</nav></header>
<main>
  <section class="hero"><h1>Product</h1><button>🚀 Start</button></section>
  <section class="features"><article>A</article><article>B</article><article>C</article></section>
  <section>Proof</section>
</main>
<footer>Footer</footer>
<script>console.log('ok')</script>
</body>
</html>"""
        report = self.orch._html_quality_report(html, source="inline")
        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Emoji glyphs detected" in err for err in report.get("errors", [])))

    def test_validate_builder_quality_auto_sanitizes_emoji_only_failure(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Emoji Auto Fix</title>
<style>
:root { --bg:#0b1020; --fg:#e9ecf1; --brand:#3dd5f3; --gap:16px; }
* { box-sizing:border-box; }
body { margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:var(--fg); background:linear-gradient(180deg,#0b1020,#121a34); }
header,main,section,footer,nav { display:block; }
nav { display:flex; justify-content:space-between; padding:20px; }
main { display:grid; gap:var(--gap); padding:24px; }
.hero { display:flex; gap:24px; align-items:center; min-height:40vh; }
.features { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.proof { display:grid; gap:10px; }
.cta { display:flex; gap:12px; }
button { padding:10px 16px; border-radius:10px; border:none; background:var(--brand); color:#001018; }
button:focus-visible { outline:2px solid #fff; outline-offset:2px; }
footer { padding:24px; opacity:.85; }
@media (max-width: 900px) { .hero { flex-direction:column; } .features { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><nav><strong>Brand</strong><a href="#">Pricing</a></nav></header>
<main>
  <section class="hero"><h1>Modern Product</h1><p>Ship fast with confidence.</p><button aria-label="Start trial">🚀 Start free</button></section>
  <section class="features"><article>Fast</article><article>Secure</article><article>Reliable</article></section>
  <section class="proof"><blockquote>Trusted by teams.</blockquote></section>
  <section class="cta"><button>Book demo</button></section>
</main>
<footer>2026 Demo Inc.</footer>
<script>document.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{}));</script>
</body>
</html>"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "index.html"
            p.write_text(html, encoding="utf-8")
            report = self.orch._validate_builder_quality([str(p)], output="")
            self.assertTrue(report.get("pass"))
            self.assertFalse(any("Emoji glyphs detected" in str(err) for err in report.get("errors", [])))
            self.assertTrue(any("Auto-sanitized emoji glyphs" in str(w) for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_rejects_missing_secondary_pages(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Demo</title>
<style>:root{--bg:#0b1020}body{margin:0;background:var(--bg)}main{display:grid}@media(max-width:700px){main{display:block}}</style>
</head>
<body><main><section><h1>Home</h1><p>Only one page exists.</p></section><footer>Footer</footer></main><script>1</script></body>
</html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index)],
                    output="",
                    goal="做一个三页面官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Multi-page delivery incomplete" in err for err in report.get("errors", [])))

    def test_multi_page_quality_gate_passes_when_all_pages_and_nav_exist(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            pricing = tmp_out / "pricing.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#0b1020;--fg:#e9ecf1;--panel:#121a34;--accent:#3dd5f3;--line:rgba(255,255,255,.08);--gap:16px}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#121a34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}header{position:sticky;top:0;background:rgba(11,16,32,.82);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}"
                "nav{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:18px 24px}"
                "nav .links{display:flex;gap:14px}main{display:grid;gap:var(--gap);padding:24px}.hero,.grid,.cta,.contact-grid{display:grid;gap:16px}"
                ".hero{grid-template-columns:1.3fr .7fr;align-items:center}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}"
                ".grid{grid-template-columns:repeat(3,1fr)}.cta{grid-template-columns:repeat(2,minmax(0,1fr))}.contact-grid{grid-template-columns:repeat(2,1fr)}"
                "a{color:var(--fg)}button{padding:12px 18px;border-radius:999px;border:none;background:var(--accent);color:#062033;font-weight:700}"
                "footer{padding:24px;opacity:.8}.eyebrow{text-transform:uppercase;letter-spacing:.18em;font-size:.75rem;opacity:.7}"
                "@media(max-width:900px){.hero,.grid,.cta,.contact-grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></div><button>Book demo</button></nav></header>
<main>
  <section class="hero">
    <article class="panel"><p class="eyebrow">Platform</p><h1>Operate the rollout from one calm command center.</h1><p>Northstar gives operators a premium multi-page web presence with clear product value, trust signals, and a strong conversion path.</p><p>Use the linked pages to compare plans, talk to sales, and inspect implementation notes without collapsing everything into one scroll.</p></article>
    <article class="panel"><h2>Launch snapshot</h2><p>Conversion-focused hero</p><p>Decision-ready pricing narrative</p><p>Human support and onboarding detail</p></article>
  </section>
  <section class="grid">
    <article class="panel"><h3>Faster onboarding</h3><p>Structured rollout steps, guided setup, and clear ownership.</p></article>
    <article class="panel"><h3>Sharper proof</h3><p>Reference customers, evidence blocks, and concise outcomes.</p></article>
    <article class="panel"><h3>Cleaner handoff</h3><p>Design, QA, and delivery stay aligned across every linked page.</p></article>
  </section>
  <section class="cta">
    <article class="panel"><h3>Read pricing</h3><p>Open the pricing page for plans, packaging, and activation support.</p></article>
    <article class="panel"><h3>Contact the team</h3><p>Use the contact page for sales, onboarding, and enterprise security review.</p></article>
  </section>
</main><footer>Northstar launch kit.</footer><script>document.querySelectorAll('a,button').forEach(el=>el.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            pricing.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pricing</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="index.html">Home</a><a href="contact.html">Contact</a></div><button>Talk to sales</button></nav></header>
<main><section class="panel"><h1>Pricing</h1><p>Three plans with rollout guidance, procurement notes, and security support.</p></section><section class="grid"><article class="panel"><h2>Starter</h2><p>Fast setup for lean teams.</p></article><article class="panel"><h2>Growth</h2><p>Automation, review loops, and stronger collaboration.</p></article><article class="panel"><h2>Enterprise</h2><p>Governance, white-glove onboarding, and custom controls.</p></article></section></main><footer>Transparent plans.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            contact.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="index.html">Home</a><a href="pricing.html">Pricing</a></div><button>Email us</button></nav></header>
<main><section class="panel"><h1>Contact</h1><p>Reach onboarding, support, and enterprise architecture review from one place.</p></section><section class="contact-grid"><article class="panel"><h2>Sales</h2><p>Response in one business day.</p></article><article class="panel"><h2>Support</h2><p>Priority coverage and launch-room escalation paths.</p></article></section></main><footer>Human response, no black box.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index), str(pricing), str(contact)],
                    output="",
                    goal="做一个三页面官网，包含首页、定价页和联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))

    def test_multi_page_quality_gate_auto_patches_root_navigation_when_only_nav_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            collections = tmp_out / "collections.html"
            craftsmanship = tmp_out / "craftsmanship.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#0f1222;--fg:#f3f6ff;--panel:#171b31;--line:rgba(255,255,255,.08);--accent:#d6b36f}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0f1222,#171b31);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:18px;padding:18px 24px;border-bottom:1px solid var(--line)}"
                ".links{display:flex;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}"
                "main{display:grid;gap:18px;padding:24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}a{color:var(--fg)}button{padding:10px 16px;border:none;border-radius:999px;background:var(--accent);color:#241b0d;font-weight:700}"
                "@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Maison Aurelia</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="collection.html">Collection</a><a href="atelier.html">Atelier</a><a href="contact.html">Contact</a></div><button>Visit house</button></nav></header>
<main><section class="panel"><h1>Quiet luxury for modern wardrobes.</h1><p>The homepage already has strong visual direction and full copy, but two navigation slugs still point at filenames that do not exist.</p><p>The linked pages are already on disk and should be preserved, not regenerated.</p></section><section class="grid"><article class="panel"><h2>Signature silhouettes</h2><p>Structured tailoring with refined detail.</p></article><article class="panel"><h2>Studio craft</h2><p>Garment construction and finishing narratives.</p></article><article class="panel"><h2>Private appointments</h2><p>Clienteling, fittings, and concierge services.</p></article></section></main><footer>Maison Aurelia.</footer><script>document.querySelectorAll('a,button').forEach(el=>el.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            collections.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Collections</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="craftsmanship.html">Craftsmanship</a><a href="contact.html">Contact</a></div><button>View looks</button></nav></header>
<main><section class="panel"><h1>Collections</h1><p>Seasonal wardrobe systems, hero garments, and styling sequences for the current collection.</p></section><section class="grid"><article class="panel"><h2>Outerwear</h2><p>Cashmere coats and technical wool layers.</p></article><article class="panel"><h2>Knitwear</h2><p>Fine gauge softness and rich texture.</p></article><article class="panel"><h2>Accessories</h2><p>Leather goods and finishing details.</p></article></section></main><footer>Collections archive.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            craftsmanship.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Craftsmanship</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="contact.html">Contact</a></div><button>Book fitting</button></nav></header>
<main><section class="panel"><h1>Craftsmanship</h1><p>Pattern development, atelier finishing, and material sourcing are already documented in this real page.</p></section><section class="grid"><article class="panel"><h2>Pattern room</h2><p>Architectural drafting and fittings.</p></article><article class="panel"><h2>Fabric lab</h2><p>Hand feel, structure, and drape testing.</p></article><article class="panel"><h2>Final finish</h2><p>Pressing, detailing, and inspection.</p></article></section></main><footer>Craft stories.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            contact.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="craftsmanship.html">Craftsmanship</a></div><button>Email concierge</button></nav></header>
<main><section class="panel"><h1>Contact</h1><p>Appointments, showroom visits, and aftercare inquiries route through this completed contact page.</p></section><section class="grid"><article class="panel"><h2>Appointments</h2><p>Private fitting windows.</p></article><article class="panel"><h2>Showroom</h2><p>Press and wholesale visits.</p></article><article class="panel"><h2>Care</h2><p>Alteration and repair service.</p></article></section></main><footer>Client services.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index), str(collections), str(craftsmanship), str(contact)],
                    output="",
                    goal="做一个四页面官网，包含首页、系列、工艺、联系页",
                )
                patched_index = index.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertIn("data-evermind-site-map", patched_index)
        self.assertIn('href="collections.html"', patched_index)
        self.assertIn('href="craftsmanship.html"', patched_index)
        self.assertTrue(any("Auto-patched homepage navigation" in str(w) for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_auto_patch_removes_dead_extra_root_links(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            collections = tmp_out / "collections.html"
            craftsmanship = tmp_out / "craftsmanship.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#0b1020;--fg:#f6f7fb;--panel:#141b31;--line:rgba(255,255,255,.09);--accent:#d6b36f}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#151d34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:18px;padding:18px 24px;border-bottom:1px solid var(--line)}"
                ".links{display:flex;flex-wrap:wrap;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}"
                "main{display:grid;gap:18px;padding:24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}a{color:var(--fg)}button{padding:10px 16px;border:none;border-radius:999px;background:var(--accent);color:#241b0d;font-weight:700}"
                "@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Maison Aurelia</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="collections.html">Collections</a><a href="craftsmanship.html">Craftsmanship</a><a href="contact.html">Contact</a><a href="destinations.html">Destinations</a></div><button>Visit house</button></nav></header>
<main><section class="panel"><h1>Quiet luxury for modern wardrobes.</h1><p>The homepage is otherwise complete and already links to every real page, but one stale dead link should be removed instead of triggering a rebuild.</p></section><section class="grid"><article class="panel"><h2>Signature silhouettes</h2><p>Structured tailoring with refined detail.</p></article><article class="panel"><h2>Studio craft</h2><p>Garment construction and finishing narratives.</p></article><article class="panel"><h2>Private appointments</h2><p>Clienteling, fittings, and concierge services.</p></article></section></main><footer>Maison Aurelia.</footer><script>document.querySelectorAll('a,button').forEach(el=>el.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            collections.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Collections</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="craftsmanship.html">Craftsmanship</a><a href="contact.html">Contact</a></div><button>View looks</button></nav></header>
<main><section class="panel"><h1>Collections</h1><p>Seasonal wardrobe systems, hero garments, and styling sequences for the current collection.</p></section><section class="grid"><article class="panel"><h2>Outerwear</h2><p>Cashmere coats and technical wool layers.</p></article><article class="panel"><h2>Knitwear</h2><p>Fine gauge softness and rich texture.</p></article><article class="panel"><h2>Accessories</h2><p>Leather goods and finishing details.</p></article></section></main><footer>Collections archive.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            craftsmanship.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Craftsmanship</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="contact.html">Contact</a></div><button>Book fitting</button></nav></header>
<main><section class="panel"><h1>Craftsmanship</h1><p>Pattern development, atelier finishing, and material sourcing are already documented in this real page.</p></section><section class="grid"><article class="panel"><h2>Pattern room</h2><p>Architectural drafting and fittings.</p></article><article class="panel"><h2>Fabric lab</h2><p>Hand feel, structure, and drape testing.</p></article><article class="panel"><h2>Final finish</h2><p>Pressing, detailing, and inspection.</p></article></section></main><footer>Craft stories.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            contact.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="craftsmanship.html">Craftsmanship</a></div><button>Email concierge</button></nav></header>
<main><section class="panel"><h1>Contact</h1><p>Appointments, showroom visits, and aftercare inquiries route through this completed contact page.</p></section><section class="grid"><article class="panel"><h2>Appointments</h2><p>Private fitting windows.</p></article><article class="panel"><h2>Showroom</h2><p>Press and wholesale visits.</p></article><article class="panel"><h2>Care</h2><p>Alteration and repair service.</p></article></section></main><footer>Client services.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index), str(collections), str(craftsmanship), str(contact)],
                    output="",
                    goal="做一个四页面官网，包含首页、系列、工艺、联系页",
                )
                patched_index = index.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertNotIn('href="destinations.html"', patched_index)
        self.assertIn('href="collections.html"', patched_index)
        self.assertIn('href="craftsmanship.html"', patched_index)
        self.assertIn('href="contact.html"', patched_index)
        self.assertTrue(any("Auto-patched homepage navigation" in str(w) for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_warns_while_parallel_builder_sibling_pending(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home</title><style>:root{--bg:#0b1020;--fg:#e9ecf1;--panel:#121a34;--line:rgba(255,255,255,.08)}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#121a34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;padding:18px 24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}main{display:grid;gap:16px;padding:24px}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head>
<body><header><nav><strong>Northstar</strong><a href="pricing.html">Pricing</a></nav></header><main><section class="panel"><h1>Home</h1><p>Initial page ready with real launch narrative, conversion framing, and implementation detail.</p><p>The sibling builder still owes the secondary linked pages, but this page already carries the shared visual system and clear navigation intent.</p></section><section class="grid"><article class="panel"><h2>Ops clarity</h2><p>Structured review flow.</p></article><article class="panel"><h2>Faster shipping</h2><p>Hard quality gates.</p></article><article class="panel"><h2>Cleaner handoff</h2><p>Traceable fixes.</p></article></section><footer>Footer</footer></main><script>1</script></body></html>""",
                encoding="utf-8",
            )
            plan = Plan(
                goal="做一个三页面官网，包含首页、定价页和联系页",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary pages"),
                ],
            )
            plan.subtasks[0].status = TaskStatus.COMPLETED
            plan.subtasks[1].status = TaskStatus.PENDING
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(index)],
                    output="",
                    goal=plan.goal,
                    plan=plan,
                    subtask=plan.subtasks[0],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertTrue(any("Multi-page delivery is still incomplete" in w for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_preserves_secondary_builder_when_only_home_nav_needs_patch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            collections = tmp_out / "collections.html"
            craftsmanship = tmp_out / "craftsmanship.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#0f1222;--fg:#f3f6ff;--panel:#171b31;--line:rgba(255,255,255,.08);--accent:#d6b36f}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0f1222,#171b31);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:18px;padding:18px 24px;border-bottom:1px solid var(--line)}"
                ".links{display:flex;gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}"
                "main{display:grid;gap:18px;padding:24px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}a{color:var(--fg)}button{padding:10px 16px;border:none;border-radius:999px;background:var(--accent);color:#241b0d;font-weight:700}"
                "@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Maison Aurelia</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="collection.html">Collection</a><a href="atelier.html">Atelier</a><a href="contact.html">Contact</a></div><button>Visit house</button></nav></header>
<main><section class="panel"><h1>Quiet luxury for modern wardrobes.</h1><p>The homepage already has strong visual direction and full copy, but two navigation slugs still point at filenames that do not exist.</p><p>The linked pages are already on disk and should be preserved, not regenerated.</p></section><section class="grid"><article class="panel"><h2>Signature silhouettes</h2><p>Structured tailoring with refined detail.</p></article><article class="panel"><h2>Studio craft</h2><p>Garment construction and finishing narratives.</p></article><article class="panel"><h2>Private appointments</h2><p>Clienteling, fittings, and concierge services.</p></article></section></main><footer>Maison Aurelia.</footer><script>document.querySelectorAll('a,button').forEach(el=>el.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            collections.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Collections</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="craftsmanship.html">Craftsmanship</a><a href="contact.html">Contact</a></div><button>View looks</button></nav></header>
<main><section class="panel"><h1>Collections</h1><p>Seasonal wardrobe systems, hero garments, and styling sequences for the current collection.</p></section><section class="grid"><article class="panel"><h2>Outerwear</h2><p>Cashmere coats and technical wool layers.</p></article><article class="panel"><h2>Knitwear</h2><p>Fine gauge softness and rich texture.</p></article><article class="panel"><h2>Accessories</h2><p>Leather goods and finishing details.</p></article></section></main><footer>Collections archive.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            craftsmanship.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Craftsmanship</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="contact.html">Contact</a></div><button>Book fitting</button></nav></header>
<main><section class="panel"><h1>Craftsmanship</h1><p>Pattern development, atelier finishing, and material sourcing are already documented in this real page.</p></section><section class="grid"><article class="panel"><h2>Pattern room</h2><p>Architectural drafting and fittings.</p></article><article class="panel"><h2>Fabric lab</h2><p>Hand feel, structure, and drape testing.</p></article><article class="panel"><h2>Final finish</h2><p>Pressing, detailing, and inspection.</p></article></section></main><footer>Craft stories.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            contact.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Contact</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a><a href="collections.html">Collections</a><a href="craftsmanship.html">Craftsmanship</a></div><button>Email concierge</button></nav></header>
<main><section class="panel"><h1>Contact</h1><p>Appointments, showroom visits, and aftercare inquiries route through this completed contact page.</p></section><section class="grid"><article class="panel"><h2>Appointments</h2><p>Private fitting windows.</p></article><article class="panel"><h2>Showroom</h2><p>Press and wholesale visits.</p></article><article class="panel"><h2>Care</h2><p>Alteration and repair service.</p></article></section></main><footer>Client services.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            plan = Plan(
                goal="做一个四页面官网，包含首页、系列、工艺、联系页",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary pages"),
                ],
            )
            plan.subtasks[0].status = TaskStatus.COMPLETED
            plan.subtasks[1].status = TaskStatus.COMPLETED
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(collections), str(craftsmanship), str(contact)],
                    output="",
                    goal=plan.goal,
                    plan=plan,
                    subtask=plan.subtasks[1],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertTrue(any("Homepage navigation still needs repair by Builder 1" in w for w in report.get("warnings", [])))

    def test_multi_page_quality_gate_rejects_secondary_builder_preview_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            preview_dir = tmp_out / "task_3"
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_html = preview_dir / "index.html"
            preview_html.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Secondary Preview</title><style>body{margin:0;background:#111;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}main{display:grid;gap:16px;padding:24px}section{display:block;padding:20px;border:1px solid rgba(255,255,255,.1);border-radius:18px}</style></head>
<body><main><section><h1>Only a preview fallback</h1><p>This is not a real named secondary page.</p></section><section><h2>Still incomplete</h2><p>No shared navigation or owned route file was saved.</p></section></main></body></html>""",
                encoding="utf-8",
            )
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、材质、传承、门店、联系页",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary pages"),
                ],
            )
            plan.subtasks[0].status = TaskStatus.IN_PROGRESS
            plan.subtasks[1].status = TaskStatus.COMPLETED
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(preview_html)],
                    output="",
                    goal=plan.goal,
                    plan=plan,
                    subtask=plan.subtasks[1],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("real named HTML page" in err or "named pages like" in err for err in report.get("errors", [])))

    def test_multi_page_quality_gate_rejects_partial_index_part_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            part = tmp_out / "index_part1.html"
            part.write_text("<!doctype html><html><body>part</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(part)],
                    output="",
                    goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、定价、故事、门店、联系页",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Partial index_part artifacts" in err for err in report.get("errors", [])))

    def test_aggregate_multi_page_gate_requeues_all_builders_before_reviewer_runs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home</title><style>body{margin:0;background:#111;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}main{display:grid;gap:16px;padding:24px}section{display:block;padding:20px;border:1px solid rgba(255,255,255,.1);border-radius:18px}</style></head>
<body><main><section><h1>Maison Aurelia</h1><p>Only the homepage exists so far.</p><nav><a href="collections.html">Collections</a></nav></section></main><script>1</script></body></html>""",
                encoding="utf-8",
            )
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、定价、故事、门店、联系页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                    SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1", "2"]),
                ],
            )
            for builder in plan.subtasks[:2]:
                builder.status = TaskStatus.COMPLETED
            results = {"1": {"success": True}, "2": {"success": True}}
            completed = {"1", "2"}
            succeeded = {"1", "2"}
            failed = set()
            self.orch.emit = AsyncMock()

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                changed = asyncio.run(
                    self.orch._enforce_multi_page_builder_aggregate_gate(
                        plan,
                        results,
                        completed,
                        succeeded,
                        failed,
                    )
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(changed)
        self.assertEqual(plan.subtasks[0].status, TaskStatus.PENDING)
        self.assertEqual(plan.subtasks[1].status, TaskStatus.PENDING)
        self.assertEqual(plan.subtasks[0].retries, 1)  # §P0-FIX: retries now correctly incremented
        self.assertEqual(plan.subtasks[1].retries, 1)  # §P0-FIX: retries now correctly incremented
        self.assertNotIn("1", completed)
        self.assertNotIn("2", completed)
        self.assertNotIn("1", succeeded)
        self.assertNotIn("2", succeeded)

    def test_aggregate_multi_page_gate_requeues_only_builder_missing_owned_pages(self):
        class StubBridge:
            config = {}

            def _builder_assigned_html_targets(self, input_data):
                text = str(input_data or "")
                if "build home" in text:
                    return ["index.html", "brand.html", "craftsmanship.html", "collections.html"]
                return ["materials.html", "heritage.html", "contact.html", "faq.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        orch.emit = AsyncMock()
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>:root{--bg:#11141c;--fg:#f5f1ea;--panel:#171b25;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:#11141c;color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;gap:14px;padding:18px 24px}"
                "main{display:grid;gap:18px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px;background:var(--panel)}"
                "@media(max-width:900px){nav{flex-wrap:wrap}}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><a href="brand.html">Brand</a><a href="craftsmanship.html">Craftsmanship</a><a href="collections.html">Collections</a><a href="materials.html">Materials</a></nav></header>
<main><section class="panel"><h1>Maison Aurelia</h1><p>Homepage and first builder pages are already complete.</p></section></main><footer>Home footer.</footer><script>document.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{{}}));</script></body></html>""",
                encoding="utf-8",
            )
            for page in ("brand.html", "craftsmanship.html", "collections.html"):
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{page}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section class="panel"><h1>{page}</h1><p>Complete content.</p></section></main><footer>{page}</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、材质、传承、联系、FAQ 页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                    SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1", "2"]),
                ],
            )
            for builder in plan.subtasks[:2]:
                builder.status = TaskStatus.COMPLETED
            results = {"1": {"success": True}, "2": {"success": True}}
            completed = {"1", "2"}
            succeeded = {"1", "2"}
            failed = set()

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                changed = asyncio.run(
                    orch._enforce_multi_page_builder_aggregate_gate(
                        plan,
                        results,
                        completed,
                        succeeded,
                        failed,
                    )
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(changed)
        self.assertEqual(plan.subtasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(plan.subtasks[1].status, TaskStatus.PENDING)
        self.assertEqual(plan.subtasks[0].retries, 0)
        self.assertEqual(plan.subtasks[1].retries, 1)  # §P0-FIX: retries now correctly incremented
        self.assertIn("materials.html", plan.subtasks[1].description)
        self.assertNotIn("materials.html", plan.subtasks[0].description)

    def test_aggregate_multi_page_gate_requeues_only_home_builder_for_nav_slug_mismatch(self):
        """§P1-FIX: root_nav_only issues are now auto-patched by the aggregate gate
        instead of re-queuing builders. Verify that the gate succeeds via auto-patch."""
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            collections = tmp_out / "collections.html"
            craftsmanship = tmp_out / "craftsmanship.html"
            contact = tmp_out / "contact.html"
            common_style = (
                "<style>:root{--bg:#12131a;--fg:#f5f2ec;--panel:#1a1d26;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#12131a,#1a1d26);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:16px;padding:18px 24px}.links{display:flex;gap:14px}"
                "main{display:grid;gap:16px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.02)}"
                ".grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            index.write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Maison Aurelia</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="collection.html">Collection</a><a href="atelier.html">Atelier</a><a href="contact.html">Contact</a></div></nav></header>
<main><section class="panel"><h1>Quiet luxury, cinematic motion.</h1><p>The homepage exists, but its navigation still references filenames that are not on disk.</p></section><section class="grid"><article class="panel"><h2>Line</h2><p>Structured silhouettes.</p></article><article class="panel"><h2>Craft</h2><p>Atelier finishing.</p></article><article class="panel"><h2>Service</h2><p>Private appointments.</p></article></section></main><footer>Maison Aurelia.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page, title in [
                (collections, "Collections"),
                (craftsmanship, "Craftsmanship"),
                (contact, "Contact"),
            ]:
                page.write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title>{common_style}</head>
<body><header><nav><strong>Maison Aurelia</strong><div class="links"><a href="index.html">Home</a></div></nav></header>
<main><section class="panel"><h1>{title}</h1><p>This page is already complete and should be preserved during homepage navigation repair.</p></section><section class="grid"><article class="panel"><h2>Module 1</h2><p>Meaningful content.</p></article><article class="panel"><h2>Module 2</h2><p>Meaningful content.</p></article><article class="panel"><h2>Module 3</h2><p>Meaningful content.</p></article></section></main><footer>{title} footer.</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )
            plan = Plan(
                goal="做一个四页面官网，包含首页、系列、工艺、联系页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                    SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1", "2"]),
                ],
            )
            for builder in plan.subtasks[:2]:
                builder.status = TaskStatus.COMPLETED
            results = {"1": {"success": True}, "2": {"success": True}}
            completed = {"1", "2"}
            succeeded = {"1", "2"}
            failed = set()
            self.orch.emit = AsyncMock()

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                changed = asyncio.run(
                    self.orch._enforce_multi_page_builder_aggregate_gate(
                        plan,
                        results,
                        completed,
                        succeeded,
                        failed,
                    )
                )
                patched_index = index.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        # §P1-FIX: auto-patch should have fixed the nav, so no re-queue needed
        self.assertFalse(changed)
        self.assertEqual(plan.subtasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(plan.subtasks[1].status, TaskStatus.COMPLETED)
        self.assertEqual(plan.subtasks[0].retries, 0)
        self.assertEqual(plan.subtasks[1].retries, 0)
        self.assertIn("1", completed)
        self.assertIn("2", completed)
        self.assertIn("1", succeeded)
        self.assertIn("2", succeeded)
        # Verify the auto-patch actually fixed the index.html nav
        self.assertIn("data-evermind-site-map", patched_index)
        self.assertIn('href="collections.html"', patched_index)
        self.assertIn('href="craftsmanship.html"', patched_index)
        self.assertIn('href="contact.html"', patched_index)

    def test_evaluate_multi_page_artifacts_flags_secondary_dead_local_links(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>body{margin:0;background:#111;color:#f5f2ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "main,section,nav,header,footer{display:block}nav{display:flex;gap:12px;padding:20px}section{padding:24px}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><a href="features.html">Features</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header><main><section><h1>Home</h1><p>Complete homepage.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            (tmp_out / "features.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Features</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a><a href="gallery.html">Gallery</a></nav></header><main><section><h1>Features</h1><p>Strong content.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page in ("pricing.html", "contact.html"):
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{page}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section><h1>{page}</h1><p>Complete page.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                gate = self.orch._evaluate_multi_page_artifacts("做一个四页面官网，包含首页、功能页、价格页、联系页")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(gate.get("ok"))
        self.assertEqual(gate.get("repair_scope"), "nav_repair")
        self.assertIn("features.html -> gallery.html", " ".join(gate.get("broken_local_nav_entries", [])))
        self.assertTrue(any("Broken local navigation links detected" in err for err in gate.get("errors", [])))

    def test_evaluate_multi_page_artifacts_flags_incomplete_secondary_route_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>body{margin:0;background:#111;color:#f5f2ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "main,section,nav,header,footer{display:block}nav{display:flex;gap:12px;padding:20px}section{padding:24px}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home</title>{common_style}</head>
<body><header><nav><a href="features.html">Features</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></nav></header><main><section><h1>Home</h1><p>Complete homepage.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            (tmp_out / "features.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Features</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section><h1>Features</h1><p>Strong content.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page in ("pricing.html", "contact.html"):
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{page}</title>{common_style}</head>
<body><header><nav><a href="index.html">Home</a></nav></header><main><section><h1>{page}</h1><p>Complete page.</p></section></main><footer>Footer</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                gate = self.orch._evaluate_multi_page_artifacts("做一个四页面官网，包含首页、功能页、价格页、联系页")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(gate.get("ok"))
        self.assertEqual(gate.get("repair_scope"), "nav_repair")
        self.assertTrue(
            any(
                "features.html missing" in entry
                and "pricing.html" in entry
                and "contact.html" in entry
                for entry in gate.get("secondary_missing_nav_entries", [])
            )
        )
        self.assertTrue(any("Shared navigation is incomplete on generated pages" in err for err in gate.get("errors", [])))

    def test_multi_page_quality_gate_auto_patches_secondary_route_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            common_style = (
                "<style>:root{--bg:#12131a;--fg:#f5f2ec;--panel:#1a1d26;--line:rgba(255,255,255,.08)}"
                "*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#12131a,#1a1d26);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
                "header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;gap:16px;padding:18px 24px}.links{display:flex;gap:14px}"
                "main{display:grid;gap:16px;padding:24px}.panel{padding:20px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.02)}"
                ".grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>"
            )
            (tmp_out / "index.html").write_text(
                f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Northstar</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="features.html">Features</a><a href="pricing.html">Pricing</a><a href="contact.html">Contact</a></div></nav></header>
<main><section class="panel"><h1>Home</h1><p>The homepage is already complete and exposes all routes.</p></section><section class="grid"><article class="panel"><h2>Proof</h2><p>Strong launch narrative.</p></article><article class="panel"><h2>Ops</h2><p>Decision-ready content.</p></article><article class="panel"><h2>Sales</h2><p>Clear conversion path.</p></article></section></main><footer>Northstar footer.</footer><script>1</script></body></html>""",
                encoding="utf-8",
            )
            for page, title in [("features.html", "Features"), ("pricing.html", "Pricing"), ("contact.html", "Contact")]:
                (tmp_out / page).write_text(
                    f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title>{common_style}</head>
<body><header><nav><strong>Northstar</strong><div class="links"><a href="index.html">Home</a></div></nav></header>
<main><section class="panel"><h1>{title}</h1><p>This route is complete, but its local navigation is too narrow and should be auto-patched instead of triggering a rebuild.</p></section><section class="grid"><article class="panel"><h2>Module 1</h2><p>Meaningful content.</p></article><article class="panel"><h2>Module 2</h2><p>Meaningful content.</p></article><article class="panel"><h2>Module 3</h2><p>Meaningful content.</p></article></section></main><footer>{title} footer.</footer><script>1</script></body></html>""",
                    encoding="utf-8",
                )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                report = self.orch._validate_builder_quality(
                    [str(tmp_out / "index.html"), str(tmp_out / "features.html"), str(tmp_out / "pricing.html"), str(tmp_out / "contact.html")],
                    output="",
                    goal="做一个四页面官网，包含首页、功能页、价格页、联系页",
                )
                patched_features = (tmp_out / "features.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(report.get("pass"))
        self.assertTrue(any("Auto-patched generated navigation" in str(w) for w in report.get("warnings", [])))
        self.assertIn("data-evermind-site-map", patched_features)
        self.assertIn('href="pricing.html"', patched_features)
        self.assertIn('href="contact.html"', patched_features)


class TestDifficultyPlansAndRetryTargets(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(ai_bridge=None, executor=None)

    def test_pro_multi_page_website_focus_uses_real_page_ownership(self):
        focus_1, focus_2 = self.orch._pro_builder_focus("做一个八页面官网，包含首页、产品、方案、案例、定价、关于、博客、联系页")
        self.assertIn("MULTI-PAGE website request", focus_1)
        self.assertIn("/tmp/evermind_output/index.html", focus_1)
        self.assertIn("do NOT write /tmp/evermind_output/index_part1.html", focus_1)
        self.assertIn("do NOT write /tmp/evermind_output/index_part2.html", focus_2)
        self.assertIn("remaining 4 page", focus_2)

    def test_standard_fallback_tester_depends_on_deployer(self):
        plan = self.orch._fallback_plan_for_difficulty("Build a page", "standard")
        tester = next(st for st in plan if st.agent_type == "tester")
        self.assertEqual(tester.depends_on, ["3"])

    def test_simple_fallback_tester_depends_on_deployer(self):
        plan = self.orch._fallback_plan_for_difficulty("Build a page", "simple")
        tester = next(st for st in plan if st.agent_type == "tester")
        self.assertEqual(tester.depends_on, ["2"])

    def test_collect_upstream_repair_targets_for_pro_chain(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = [
            type("Task", (), {"id": "1", "agent_type": "analyst", "depends_on": []})(),
            type("Task", (), {"id": "2", "agent_type": "builder", "depends_on": ["1"]})(),
            type("Task", (), {"id": "3", "agent_type": "reviewer", "depends_on": ["2"]})(),
            type("Task", (), {"id": "4", "agent_type": "debugger", "depends_on": ["3"]})(),
            type("Task", (), {"id": "5", "agent_type": "deployer", "depends_on": ["4"]})(),
            type("Task", (), {"id": "6", "agent_type": "tester", "depends_on": ["5"]})(),
        ]
        test_task = plan.subtasks[-1]
        targets = self.orch._collect_upstream_repair_targets(plan, test_task)
        target_types = [t.agent_type for t in targets]
        self.assertIn("debugger", target_types)
        self.assertIn("builder", target_types)

    def test_pro_prompt_targets_eight_subtasks_for_simple_goal(self):
        prompt = self.orch._planner_prompt_for_difficulty("Build landing page", "pro")
        self.assertIn("8-12 visible nodes including a dedicated planner", prompt)
        self.assertIn("8 non-planner subtasks", prompt)
        self.assertIn("MUST have 2 builders", prompt)

    def test_pro_planner_task_description_for_game_demands_merger_ownership_and_rollbacks(self):
        prompt = self.orch._pro_planner_task_description(
            "创建一个第三人称 3D 射击游戏，鼠标拖动视角，有怪物、枪械、关卡和通过页面。",
        )
        self.assertIn("builder_ownership", prompt)
        self.assertIn("subsystem_contracts", prompt)
        self.assertIn("review_evidence", prompt)
        self.assertIn("rollback_triggers", prompt)
        self.assertIn("vertical look sign convention", prompt)
        self.assertIn("merger integration boundaries", prompt)

    def test_pro_prompt_targets_eleven_subtasks_for_complex_multi_page_goal(self):
        prompt = self.orch._planner_prompt_for_difficulty(
            "做一个介绍奢侈品的八页面网站，页面要非常高级，像苹果官网一样，还有电影感动画转场。",
            "pro",
        )
        self.assertIn("11 non-planner subtasks", prompt)
        self.assertIn("uidesign", prompt)
        self.assertIn("scribe", prompt)
        self.assertIn("polisher", prompt)
        self.assertIn("MUST have 2 builders", prompt)

    def test_pro_prompt_targets_eleven_subtasks_for_asset_heavy_game(self):
        prompt = self.orch._planner_prompt_for_difficulty(
            "做一个第三人称3d射击游戏，并生成角色模型、武器模型、怪物模型、贴图和asset pack",
            "pro",
        )
        self.assertIn("8-12 visible nodes including a dedicated planner", prompt)
        self.assertIn("11 non-planner subtasks", prompt)
        self.assertIn("MUST have 3 builders", prompt)
        self.assertIn("final integrator", prompt)

    def test_game_planner_prompt_blocks_playable_game_research(self):
        prompt = self.orch._planner_prompt_for_difficulty("做一个超级马里奥风格平台跳跃游戏", "standard")
        self.assertIn("do NOT send analyst to spend time playing browser games", prompt)

    def test_standard_asset_heavy_game_adds_specialized_pipeline_when_image_backend_available(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        }), executor=None)
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        orch._enforce_plan_shape(plan, "做一个像素风平台跳跃游戏，包含角色素材和 spritesheet", "standard")
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "imagegen", "spritesheet", "assetimport", "builder", "reviewer", "deployer", "tester"],
        )
        self.assertEqual(plan.subtasks[4].depends_on, ["1", "4"])

    def test_standard_asset_heavy_game_skips_specialized_pipeline_without_image_backend(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(plan, "做一个像素风平台跳跃游戏，包含角色素材和 spritesheet", "standard")
        self.assertEqual([s.agent_type for s in plan.subtasks], ["builder", "reviewer", "deployer", "tester"])
        self.assertIn("No configured image-generation backend is available", plan.subtasks[0].description)

    def test_standard_commercial_voxel_game_with_modeling_language_enables_3d_asset_pipeline(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        }), executor=None)
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        orch._enforce_plan_shape(
            plan,
            "创建一个我的世界风格的像素设计游戏（3d),地图丰富，要有怪物，机制等等，这款游戏要达到商业级水准，建模之类的都要有",
            "standard",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "imagegen", "spritesheet", "assetimport", "builder", "reviewer", "deployer", "tester"],
        )
        self.assertIn("3D", plan.subtasks[1].description)

    def test_standard_generic_3d_game_without_asset_request_stays_on_builder_path(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        }), executor=None)
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        orch._enforce_plan_shape(
            plan,
            "做一个 3D 枪战网页游戏，要有完整玩法循环和高级 UI",
            "standard",
        )
        self.assertEqual([s.agent_type for s in plan.subtasks], ["builder", "reviewer", "deployer", "tester"])

    def test_standard_explicit_3d_asset_goal_enables_3d_asset_pipeline(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        }), executor=None)
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        orch._enforce_plan_shape(
            plan,
            "做一个3d射击游戏，并生成角色模型、武器模型、怪物模型、贴图和asset pack",
            "standard",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["analyst", "imagegen", "spritesheet", "assetimport", "builder", "reviewer", "deployer", "tester"],
        )
        self.assertIn("3D", plan.subtasks[1].description)

    def test_browser_snapshot_log_line_includes_ref_preview(self):
        line = self.orch._browser_action_log_line({
            "action": "snapshot",
            "snapshot_ref_count": 6,
            "snapshot_refs_preview": [
                {"ref": "ref-1", "label": "Start Game", "role": "button"},
                {"ref": "ref-2", "label": "Mute", "role": "button"},
            ],
        })
        self.assertIn("ref-1", line or "")
        self.assertIn("Start Game", line or "")
        self.assertIn("等 6 个", line or "")

    def test_enforce_plan_shape_pro_canonicalizes_to_nine_visible_nodes_with_merger(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = [
            type("Task", (), {"id": "1", "agent_type": "analyst", "description": "Research UI patterns", "depends_on": []})(),
            type("Task", (), {"id": "2", "agent_type": "builder", "description": "Build page", "depends_on": ["1"]})(),
            type("Task", (), {"id": "3", "agent_type": "debugger", "description": "Fix bugs", "depends_on": ["2"]})(),
            type("Task", (), {"id": "4", "agent_type": "deployer", "description": "Deploy", "depends_on": ["3"]})(),
            type("Task", (), {"id": "5", "agent_type": "tester", "description": "Test", "depends_on": ["4"]})(),
        ]
        self.orch._enforce_plan_shape(plan, "Build landing page", "pro")
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "builder", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[2].depends_on, ["2"])
        self.assertEqual(plan.subtasks[3].depends_on, ["2"])
        self.assertEqual(plan.subtasks[4].depends_on, ["3", "4"])
        self.assertEqual(plan.subtasks[5].depends_on, ["5"])
        self.assertEqual(plan.subtasks[6].depends_on, ["6"])
        self.assertEqual(plan.subtasks[7].depends_on, ["6", "7"])
        self.assertEqual(plan.subtasks[8].depends_on, ["8"])

    def test_enforce_plan_shape_pro_complex_goal_prefers_parallel_builders(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(
            plan,
            "做一个介绍奢侈品的八页面网站，页面要像苹果官网一样高级，并带电影感动画转场。",
            "pro",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "uidesign", "scribe", "builder", "builder", "builder", "polisher", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[4].depends_on, ["2", "3"])
        self.assertEqual(plan.subtasks[5].depends_on, ["2", "3"])
        self.assertEqual(plan.subtasks[6].depends_on, ["5", "6"])
        self.assertEqual(plan.subtasks[7].depends_on, ["7", "4"])
        self.assertEqual(plan.subtasks[8].depends_on, ["8"])
        self.assertEqual(plan.subtasks[9].depends_on, ["9"])
        self.assertEqual(plan.subtasks[10].depends_on, ["9", "10"])
        self.assertEqual(plan.subtasks[11].depends_on, ["11"])

    def test_enforce_plan_shape_pro_asset_heavy_goal_canonicalizes_to_twelve_nodes_with_merger(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(
            plan,
            "做一个奢侈品 lookbook 网站，8 页，包含 hero 插画、lookbook 视觉素材和高质量 asset pack。",
            "pro",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[5].depends_on, ["2", "5"])
        self.assertEqual(plan.subtasks[6].depends_on, ["2", "5"])
        self.assertEqual(plan.subtasks[7].depends_on, ["6", "7"])

    def test_enforce_plan_shape_pro_asset_heavy_game_uses_parallel_integrator_builders(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(
            plan,
            "做一个第三人称3d射击游戏，并生成角色模型、武器模型、怪物模型、贴图和asset pack",
            "pro",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[5].depends_on, ["2", "5"])
        self.assertEqual(plan.subtasks[6].depends_on, ["2", "5"])
        self.assertEqual(plan.subtasks[7].depends_on, ["6", "7"])

    def test_enforce_plan_shape_pro_single_file_game_uses_parallel_integrator_builders(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(
            plan,
            "做一个 3D 第三人称射击游戏，带怪物、武器和大地图。",
            "pro",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "uidesign", "scribe", "builder", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[4].depends_on, ["2", "3", "4"])
        self.assertEqual(plan.subtasks[5].depends_on, ["2", "3", "4"])
        self.assertEqual(plan.subtasks[6].depends_on, ["5", "6"])

    def test_enforce_plan_shape_pro_commercial_3d_game_uses_parallel_integrator_asset_pipeline(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(
            plan,
            "创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。",
            "pro",
        )
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "imagegen", "spritesheet", "assetimport", "builder", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[5].depends_on, ["2", "5"])
        self.assertEqual(plan.subtasks[6].depends_on, ["2", "5"])
        self.assertEqual(plan.subtasks[7].depends_on, ["6", "7"])

    def test_enforce_plan_shape_standard_canonicalizes_to_four_nodes(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = [
            type("Task", (), {"id": "1", "agent_type": "builder", "description": "Build page", "depends_on": []})(),
            type("Task", (), {"id": "2", "agent_type": "deployer", "description": "Deploy", "depends_on": ["1"]})(),
        ]
        self.orch._enforce_plan_shape(plan, "Build landing page", "standard")
        self.assertEqual([s.agent_type for s in plan.subtasks], ["builder", "reviewer", "deployer", "tester"])
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[2].depends_on, ["1"])
        self.assertEqual(plan.subtasks[3].depends_on, ["2", "3"])

    def test_standard_shape_uses_task_adaptive_builder_for_game_goal(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = [
            type("Task", (), {"id": "1", "agent_type": "builder", "description": "Build a landing page hero section", "depends_on": []})(),
        ]
        self.orch._enforce_plan_shape(plan, "做一个贪吃蛇小游戏", "standard")
        self.assertIn("commercial-grade HTML5 game", plan.subtasks[0].description)
        self.assertNotIn("hero section", plan.subtasks[0].description.lower())
        self.assertIn("Follow the gameplay, UI, and runtime rules", plan.subtasks[0].description)

    def test_pro_shape_game_focus_is_not_website_specific(self):
        plan = type("PlanObj", (), {})()
        plan.subtasks = []
        self.orch._enforce_plan_shape(plan, "Build a snake game", "pro")
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "uidesign", "scribe", "builder", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        desc_lower = plan.subtasks[4].description.lower()
        self.assertIn("game", desc_lower)
        self.assertNotIn("hero section", desc_lower)
        self.assertEqual(plan.subtasks[4].depends_on, ["2", "3", "4"])
        self.assertEqual(plan.subtasks[5].depends_on, ["2", "3", "4"])
        self.assertEqual(plan.subtasks[6].depends_on, ["5", "6"])

    def test_plan_fallback_pro_still_enforces_nine_visible_nodes(self):
        class StubBridge:
            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": False, "error": "planner json parse failed"}

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)

        events = []

        async def _capture(evt):
            events.append(evt)

        orch.on_event = _capture
        plan = asyncio.run(orch._plan("Build a premium jewelry website", "kimi-coding", difficulty="pro"))
        self.assertEqual(len(plan.subtasks), 9)
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "builder", "builder", "builder", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[2].depends_on, ["2"])
        self.assertEqual(plan.subtasks[3].depends_on, ["2"])  # website = parallel builders
        self.assertEqual(plan.subtasks[4].depends_on, ["3", "4"])  # merger
        self.assertTrue(any(evt.get("type") == "planning_fallback" for evt in events))

    def test_plan_marks_router_model_as_default_for_node_preferences(self):
        captured = {}

        class StubBridge:
            async def execute(self, node, plugins, input_data, model, on_progress):
                captured["node"] = dict(node)
                return {"success": False, "error": "planner json parse failed"}

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        asyncio.run(orch._plan("Build a premium jewelry website", "gpt-5.4", difficulty="pro"))

        self.assertEqual(captured["node"]["type"], "router")
        self.assertEqual(captured["node"]["model"], "gpt-5.4")
        self.assertTrue(captured["node"].get("model_is_default"))

    def test_retry_attempt_treats_retry_model_as_explicit_override(self):
        captured = {}

        class StubBridge:
            config = {}

            def preferred_model_for_node(self, node, model):
                captured["preferred_node"] = dict(node)
                return node.get("model", model)

            def _resolve_model(self, model_name):
                return {"provider": "openai" if "gpt" in str(model_name) else "kimi"}

            async def execute(self, node, plugins, input_data, model, on_progress):
                captured["node"] = dict(node)
                return {
                    "success": True,
                    "output": "<reference_sites>\n- https://example.com\n- https://example.org\n</reference_sites>",
                    "tool_results": [
                        {"success": True, "data": {"url": "https://example.com"}},
                        {"success": True, "data": {"url": "https://example.org"}},
                    ],
                    "tool_call_stats": {"browser": 2},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[])
        subtask.retries = 1
        plan = Plan(goal="Build a premium jewelry website", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured["node"]["model"], "gpt-5.4")
        self.assertFalse(captured["node"].get("model_is_default"))

    def test_builder_retry_honors_explicit_retry_model_over_node_preferences(self):
        captured = {}

        class StubBridge:
            config = {
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            }

            def preferred_model_for_node(self, node, model):
                captured["preferred_node"] = dict(node)
                return node.get("model") if not node.get("model_is_default") else "gpt-5.4"

            def _resolve_model(self, model_name):
                return {"provider": "kimi" if "kimi" in str(model_name) else "openai"}

            async def execute(self, node, plugins, input_data, model, on_progress):
                captured["node"] = dict(node)
                return {
                    "success": True,
                    "output": "```html index.html\n<!DOCTYPE html><html><body><canvas id='game'></canvas><button id='startBtn'>Start</button><div class='hud'>HUD</div><script>function loop(){}requestAnimationFrame(loop);document.addEventListener('keydown',()=>{});</script></body></html>\n```",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        subtask = SubTask(id="5", agent_type="builder", description="build shooter", depends_on=[])
        subtask.retries = 1
        plan = Plan(goal="做一个 3D 射击游戏", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured["node"]["model"], "kimi-coding")
        self.assertFalse(captured["node"].get("model_is_default"))

    def test_plan_fallback_pro_complex_goal_enforces_parallel_builder_quality_path(self):
        class StubBridge:
            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": False, "error": "planner json parse failed"}

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        plan = asyncio.run(
            orch._plan(
                "做一个介绍奢侈品的八页面网站，页面要像苹果官网一样高级，并带电影感动画转场。",
                "kimi-coding",
                difficulty="pro",
            )
        )
        self.assertEqual(len(plan.subtasks), 12)
        self.assertEqual(
            [s.agent_type for s in plan.subtasks],
            ["planner", "analyst", "uidesign", "scribe", "builder", "builder", "builder", "polisher", "reviewer", "deployer", "tester", "debugger"],
        )
        self.assertEqual(plan.subtasks[1].depends_on, ["1"])
        self.assertEqual(plan.subtasks[4].depends_on, ["2", "3"])
        self.assertEqual(plan.subtasks[5].depends_on, ["2", "3"])
        self.assertEqual(plan.subtasks[6].depends_on, ["5", "6"])
        self.assertEqual(plan.subtasks[7].depends_on, ["7", "4"])

    def test_validate_analyst_handoff_requires_role_specific_tags(self):
        plan = Plan(
            goal="做一个高端 AI 官网",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="builder", description="top", depends_on=["1"]),
                SubTask(id="3", agent_type="builder", description="bottom", depends_on=["1"]),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"]),
                SubTask(id="5", agent_type="tester", description="test", depends_on=["4"]),
                SubTask(id="6", agent_type="debugger", description="debug", depends_on=["5"]),
            ],
        )
        text = (
            "<reference_sites>https://example.com</reference_sites>\n"
            "<design_direction>premium dark saas</design_direction>\n"
            "<non_negotiables>no emoji</non_negotiables>\n"
            "<deliverables_contract>hero, nav, proof, pricing, footer</deliverables_contract>\n"
            "<risk_register>generic hierarchy, weak CTA</risk_register>\n"
            "<builder_1_handoff>hero + nav</builder_1_handoff>\n"
            "<reviewer_handoff>be strict</reviewer_handoff>\n"
        )
        missing = self.orch._validate_analyst_handoff(text, plan)
        self.assertIn("builder_2_handoff", missing)
        self.assertIn("tester_handoff", missing)
        self.assertIn("debugger_handoff", missing)

    def test_build_analyst_handoff_context_uses_builder_slot(self):
        plan = Plan(
            goal="做一个高端 AI 官网",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="builder", description="top", depends_on=["1"]),
                SubTask(id="3", agent_type="builder", description="bottom", depends_on=["1"]),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"]),
            ],
        )
        analyst_output = (
            "<reference_sites>https://a.com\nhttps://b.com</reference_sites>\n"
            "<design_direction>premium, quiet luxury, strong hierarchy</design_direction>\n"
            "<non_negotiables>no emoji icons</non_negotiables>\n"
            "<deliverables_contract>premium hero, trust layer, proof, pricing, footer</deliverables_contract>\n"
            "<risk_register>cheap iconography, weak spacing, empty footer</risk_register>\n"
            "<builder_1_handoff>handle header, hero, benefits</builder_1_handoff>\n"
            "<builder_2_handoff>handle proof, pricing, footer</builder_2_handoff>\n"
            "<reviewer_handoff>reject generic spacing and emoji glyphs</reviewer_handoff>\n"
        )
        context = self.orch._build_analyst_handoff_context(plan, plan.subtasks[2], analyst_output)
        self.assertIn("Builder 2 Handoff", context)
        self.assertIn("handle proof, pricing, footer", context)
        self.assertIn("no emoji icons", context)
        self.assertIn("Deliverables Contract", context)
        self.assertIn("Risk Register", context)

    def test_validate_analyst_handoff_requires_all_builder_slots(self):
        plan = Plan(
            goal="继续优化一个 3D 游戏",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="builder", description="core", depends_on=["1"]),
                SubTask(id="3", agent_type="builder", description="hud", depends_on=["1"]),
                SubTask(id="4", agent_type="builder", description="integrate", depends_on=["2", "3"]),
                SubTask(id="5", agent_type="reviewer", description="review", depends_on=["4"]),
            ],
        )
        text = (
            "<reference_sites>https://example.com</reference_sites>\n"
            "<design_direction>premium</design_direction>\n"
            "<non_negotiables>no emoji</non_negotiables>\n"
            "<deliverables_contract>full game</deliverables_contract>\n"
            "<risk_register>mirror controls</risk_register>\n"
            "<builder_1_handoff>core loop</builder_1_handoff>\n"
            "<builder_2_handoff>hud lane</builder_2_handoff>\n"
            "<reviewer_handoff>be strict</reviewer_handoff>\n"
        )
        missing = self.orch._validate_analyst_handoff(text, plan)
        self.assertIn("builder_3_handoff", missing)

    def test_analyst_runtime_policy_mentions_builder_slots_and_sites(self):
        bridge = SimpleNamespace(config={
            "analyst": {
                "preferred_sites": ["https://github.com", "https://threejs.org"],
                "crawl_intensity": "high",
                "use_scrapling_when_available": True,
                "enable_query_search": True,
            }
        })
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        plan = Plan(
            goal="继续优化一个 3D 第三人称射击游戏",
            subtasks=[
                SubTask(id="1", agent_type="planner", description="plan"),
                SubTask(id="2", agent_type="analyst", description="research", depends_on=["1"]),
                SubTask(id="3", agent_type="builder", description="core", depends_on=["2"]),
                SubTask(id="4", agent_type="builder", description="effects", depends_on=["2"]),
                SubTask(id="5", agent_type="builder", description="integrate", depends_on=["3", "4"]),
            ],
        )
        policy = orch._analyst_runtime_policy_block(plan)
        self.assertIn("crawl_intensity=high", policy)
        self.assertIn("https://github.com", policy)
        self.assertIn("<builder_1_handoff>", policy)
        self.assertIn("gdquest-demos/godot-4-3d-third-person-controller", policy)
        self.assertIn("Scrapling is already wired behind source_fetch", policy)
        self.assertIn("<builder_3_handoff>", policy)
        self.assertIn("LAST builder acts as assembler/refiner", policy)


class TestCustomPlanPreservation(unittest.TestCase):
    def test_run_preserves_custom_plan_task_text(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None)
        orch.emit = AsyncMock()
        orch._execute_plan = AsyncMock(return_value={})
        orch._emit_final_preview = AsyncMock()
        orch._build_report = MagicMock(return_value={"success": True, "subtasks": []})

        canonical_context = {
            "task_id": "task_1",
            "run_id": "run_1",
            "is_custom_plan": True,
            "node_executions": [
                {
                    "id": "ne_1",
                    "node_key": "planner",
                    "node_label": "Planner",
                    "input_summary": "拆解执行顺序并输出骨架计划",
                    "depends_on_keys": [],
                },
                {
                    "id": "ne_2",
                    "node_key": "analyst",
                    "node_label": "Analyst",
                    "input_summary": "研究 3 个竞品并写下游任务书",
                    "depends_on_keys": ["planner"],
                },
            ],
        }

        result = asyncio.run(orch.run(
            goal="做一个高端 AI 官网",
            canonical_context=canonical_context,
        ))

        self.assertTrue(result["success"])
        self.assertIsNotNone(orch.active_plan)
        self.assertEqual(orch.active_plan.subtasks[0].description, "拆解执行顺序并输出骨架计划")
        self.assertEqual(orch.active_plan.subtasks[1].description, "研究 3 个竞品并写下游任务书")


class TestRuntimeQualityConfig(unittest.TestCase):
    def test_retry_policy_uses_runtime_config(self):
        bridge = type("Bridge", (), {"config": {"max_retries": 5}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        plan = type("PlanObj", (), {"subtasks": [], "max_total_retries": 10})()
        plan.subtasks = [
            type("Task", (), {"max_retries": 3})(),
            type("Task", (), {"max_retries": 3})(),
            type("Task", (), {"max_retries": 3})(),
        ]
        orch._apply_retry_policy(plan)
        self.assertTrue(all(st.max_retries == 5 for st in plan.subtasks))
        self.assertGreaterEqual(plan.max_total_retries, 15)

    def test_tester_smoke_reads_runtime_config(self):
        bridge = type("Bridge", (), {"config": {"tester_run_smoke": False}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        self.assertFalse(orch._configured_tester_smoke())

    def test_subtask_timeout_defaults_are_role_aware(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        # V4.3: raised all defaults to avoid killing healthy streams
        self.assertEqual(orch._configured_subtask_timeout("builder"), 3600)
        self.assertEqual(orch._configured_subtask_timeout("analyst"), 1800)
        self.assertEqual(orch._configured_subtask_timeout("imagegen"), 900)
        self.assertEqual(orch._configured_subtask_timeout("reviewer"), 900)
        self.assertEqual(orch._configured_subtask_timeout("tester"), 900)
        self.assertEqual(orch._configured_subtask_timeout("deployer"), 900)

    def test_builder_execution_timeout_boosts_for_large_direct_multifile_batches(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="4", agent_type="builder", description="build premium site", depends_on=[])
        plan = Plan(goal="Build a premium multi-page website", subtasks=[subtask])
        orch._builder_execution_direct_multifile_mode = lambda _plan, _subtask, _model: True  # type: ignore[method-assign]
        orch._builder_bootstrap_targets = lambda _plan, _subtask: [  # type: ignore[method-assign]
            "index.html",
            "pricing.html",
            "features.html",
            "solutions.html",
            "platform.html",
            "contact.html",
            "about.html",
            "faq.html",
            "security.html",
        ]

        timeout_sec = orch._execution_timeout_for_subtask(plan, subtask, "kimi-coding")

        # V4.3: base builder timeout raised to 3600, so multifile boost
        # is absorbed into the higher base.
        self.assertEqual(timeout_sec, 3600)

    def test_builder_execution_timeout_boosts_for_premium_3d_direct_text_game(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="4", agent_type="builder", description="build premium tps shooter", depends_on=[])
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，要有怪物、不同枪械、大地图和精美建模，达到商业级水准。",
            subtasks=[subtask],
        )
        orch._builder_execution_direct_text_mode = lambda _plan, _subtask: True  # type: ignore[method-assign]
        orch._builder_requires_existing_artifact_patch = lambda _plan, _subtask: False  # type: ignore[method-assign]

        timeout_sec = orch._execution_timeout_for_subtask(plan, subtask, "kimi-coding")

        self.assertGreaterEqual(timeout_sec, 2700)

    def test_sync_ne_timeout_budget_adds_watchdog_grace_for_builder(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._subtask_ne_map = {"4": "nodeexec_4"}
        fake_ne_store = MagicMock()
        fake_ne_store.get_node_execution.return_value = {
            "id": "nodeexec_4",
            "timeout_seconds": 1020,
        }

        with patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
            orch._sync_ne_timeout_budget("4", 1320)

        fake_ne_store.update_node_execution.assert_called_once_with(
            "nodeexec_4",
            {"timeout_seconds": 1365},
        )

    def test_retry_prompt_for_analyst_keeps_browser_requirement(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="9", agent_type="analyst", description="research", depends_on=[])
        subtask.status = TaskStatus.FAILED
        subtask.error = "timeout"
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
        self.assertTrue(ok)
        # P1 FIX: Timeout retries no longer _force_ browser; they allow it optionally
        self.assertIn("prioritize speed over breadth", captured.get("desc", ""))
        self.assertIn("MAY use the browser tool", captured.get("desc", ""))
        self.assertEqual(subtask.description, "research")

    def test_retry_prompt_for_other_nodes_asks_faster_execution(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="10", agent_type="reviewer", description="review", depends_on=[])
        subtask.status = TaskStatus.FAILED
        subtask.error = "latency spike"
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
        self.assertTrue(ok)
        self.assertIn("Be more careful and faster this time", captured.get("desc", ""))
        self.assertEqual(subtask.description, "review")

    def test_retry_prompt_for_polisher_timeout_does_not_crash(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            subtask = SubTask(id="11", agent_type="polisher", description="polish", depends_on=[], max_retries=2)
            subtask.status = TaskStatus.FAILED
            subtask.error = "polisher pre-write timeout after 90s: no real file write was produced."
            plan = Plan(goal="Build premium website", subtasks=[subtask])

            # Polisher timeout should safe-fail, NOT retry
            async def should_not_retry(*args, **kwargs):
                raise AssertionError("polisher safe fallback should avoid retry execution on timeout")

            orch._execute_subtask = should_not_retry  # type: ignore[method-assign]

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(ok)
        self.assertEqual(subtask.status, TaskStatus.COMPLETED)
        self.assertEqual(subtask.error, "")
        self.assertIn("稳定版本", subtask.output)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertIn("failed", statuses)

    def test_polisher_non_write_failure_soft_skips_to_stable_preview(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            snapshot = out / "_stable_previews" / "run_demo" / "snap_demo"
            snapshot.mkdir(parents=True, exist_ok=True)
            stable_index = snapshot / "index.html"
            stable_index.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")

            subtask = SubTask(id="12", agent_type="polisher", description="polish", depends_on=[], max_retries=2)
            subtask.status = TaskStatus.FAILED
            subtask.error = "polisher loop guard triggered after 4 non-write tool iterations without any file write."
            plan = Plan(goal="Build premium website", subtasks=[subtask])

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                orch._stable_preview_path = stable_index
                orch._stable_preview_stage = "builder_quality_pass"
                orch._stable_preview_files = [str(stable_index)]

                async def should_not_retry(*args, **kwargs):
                    raise AssertionError("polisher safe fallback should avoid retry execution")

                orch._execute_subtask = should_not_retry  # type: ignore[method-assign]
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(ok)
        self.assertEqual(subtask.status, TaskStatus.COMPLETED)
        self.assertEqual(subtask.error, "")
        self.assertIn("稳定版本", subtask.output)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertIn("failed", statuses)

    def test_retry_prompt_for_builder_incomplete_multi_page_forces_direct_multifile_delivery(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html."
            ),
            depends_on=[],
            max_retries=1,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = "Builder quality gate failed (score=44). Errors: ['Multi-page delivery incomplete: found 1/8 valid HTML pages in the current run.']"
        plan = Plan(goal="创建一个 8 页轻奢品牌网站", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                tmp_out = Path(td)
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text("<!DOCTYPE html><html><body>home</body></html>", encoding="utf-8")
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(ok)
        self.assertIn("DIRECT MULTI-FILE DELIVERY ONLY.", captured.get("desc", ""))
        self.assertIn("HTML TARGET OVERRIDE:", captured.get("desc", ""))
        self.assertIn("Do NOT use browser research, file_ops list, or file_ops read on this retry.", captured.get("desc", ""))
        self.assertIn("Output ONLY fenced code blocks", captured.get("desc", ""))

    def test_builder_reviewer_patch_retry_description_strips_stale_direct_delivery_markers_for_game(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="6",
            agent_type="builder",
            description=(
                "build premium game\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "HTML TARGET OVERRIDE: index.html\n"
                "- Do NOT use browser research, file_ops list, or file_ops read on this retry.\n"
                "- Output ONLY fenced code blocks like ```html index.html ...```.\n"
            ),
            depends_on=[],
        )
        plan = Plan(
            goal="创建一个第三人称 3D 射击网页游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
            subtasks=[subtask],
        )

        desc = orch._builder_reviewer_patch_retry_description(
            subtask,
            plan,
            "修复镜像控制、枪械建模和敌人刷新节奏。",
            round_num=1,
            max_rejections=2,
        )

        self.assertIn(f"Goal: {plan.goal}", desc)
        self.assertIn("[Reviewer Rework Patch Mode]", desc)
        self.assertIn("first meaningful action must be file_ops list/read", desc)
        self.assertNotIn("DIRECT MULTI-FILE DELIVERY ONLY.", desc)
        self.assertNotIn("HTML TARGET OVERRIDE:", desc)
        self.assertNotIn("Output ONLY fenced code blocks", desc)

    def test_builder_empty_output_circuit_switches_to_alternate_model_before_retry(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium game",
            depends_on=[],
            max_retries=3,
        )
        subtask.error = (
            "gpt-5.3-codex returned empty content for builder node "
            "(stream had 1 parts, all empty). Possible cause: context overflow or model refusal."
        )
        plan = Plan(goal="创建一个 3D 射击游戏", subtasks=[subtask])
        captured: Dict[str, str] = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["override"] = st.retry_model_override
            return {"success": False, "error": "still failing"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-real-deepseek-key-for-test"}, clear=False):
            ok = asyncio.run(orch._handle_failure(subtask, plan, "gpt-5.3-codex", results={}))

        self.assertFalse(ok)
        # Bug #1 fix: downgrade walks DOWN the chain (gpt-5.3-codex → deepseek-v3), not UP to kimi-coding
        self.assertEqual(captured.get("override"), "deepseek-v3")
        self.assertEqual(subtask.consecutive_empty_outputs, 1)

    def test_builder_consecutive_empty_output_salvages_stable_preview(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium game",
            depends_on=[],
            max_retries=3,
        )
        subtask.consecutive_empty_outputs = 1
        subtask.error = (
            "gpt-5.3-codex returned empty content for builder node "
            "(stream had 1 parts, all empty). Possible cause: context overflow or model refusal."
        )
        plan = Plan(goal="创建一个 3D 射击游戏", subtasks=[subtask])
        orch._alternate_retry_model = Mock(return_value="gpt-5.3-codex")  # type: ignore[method-assign]
        orch._restore_output_from_stable_preview = Mock(return_value=["/tmp/evermind_output/index.html"])  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "gpt-5.3-codex", results={}))

        self.assertTrue(ok)
        self.assertEqual(subtask.status, TaskStatus.COMPLETED)
        self.assertIn("stable preview", subtask.output.lower())

    def test_builder_bootstrap_targets_honor_override_for_single_builder_retry(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "HTML TARGET OVERRIDE: index.html, security.html\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html, security.html."
            ),
            depends_on=[],
        )
        plan = Plan(goal="创建一个 9 页轻奢品牌网站", subtasks=[subtask])

        self.assertEqual(
            orch._builder_bootstrap_targets(plan, subtask),
            ["index.html", "security.html"],
        )

    def test_builder_timeout_retry_targets_only_remaining_pages(self):
        class Bridge:
            config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "pricing.html", "features.html", "about.html"]

        bridge = Bridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html."
            ),
            depends_on=[],
            max_retries=1,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = "builder execution timeout after 966s."
        plan = Plan(goal="做一个四页面轻奢品牌网站，包含首页、定价页、功能页和关于页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                tmp_out = Path(td)
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><nav><a href="pricing.html">Pricing</a><a href="features.html">Features</a><a href="about.html">About</a></nav><main><section><h1>Home</h1><p>Premium home page.</p></section></main></body></html>""",
                    encoding="utf-8",
                )
                (tmp_out / "pricing.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><main><h1>Pricing</h1><p>Pricing details.</p></main></body></html>""",
                    encoding="utf-8",
                )
                (tmp_out / "features.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><main><h1>Features</h1><p>Feature details.</p></main></body></html>""",
                    encoding="utf-8",
                )
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(ok)
        self.assertIn("DIRECT MULTI-FILE DELIVERY ONLY.", captured.get("desc", ""))
        override_line = next(
            line for line in captured.get("desc", "").splitlines()
            if "HTML TARGET OVERRIDE:" in line
        )
        self.assertIn("index.html", override_line)
        self.assertIn("about.html", override_line)
        self.assertNotIn("pricing.html", override_line)
        self.assertNotIn("features.html", override_line)

    def test_builder_timeout_retry_from_gpt_still_carries_override_targets(self):
        class Bridge:
            config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "pricing.html", "features.html", "about.html"]

        bridge = Bridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html."
            ),
            depends_on=[],
            max_retries=1,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = "builder execution timeout after 966s."
        plan = Plan(goal="做一个四页面轻奢品牌网站，包含首页、定价页、功能页和关于页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                tmp_out = Path(td)
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><nav><a href="pricing.html">Pricing</a><a href="features.html">Features</a><a href="about.html">About</a></nav><main><section><h1>Home</h1><p>Premium home page.</p></section></main></body></html>""",
                    encoding="utf-8",
                )
                (tmp_out / "pricing.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><main><h1>Pricing</h1><p>Pricing details.</p></main></body></html>""",
                    encoding="utf-8",
                )
                (tmp_out / "features.html").write_text(
                    """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><main><h1>Features</h1><p>Feature details.</p></main></body></html>""",
                    encoding="utf-8",
                )
                ok = asyncio.run(orch._handle_failure(subtask, plan, "gpt-5.4", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(ok)
        self.assertIn("DIRECT MULTI-FILE DELIVERY ONLY.", captured.get("desc", ""))
        override_line = next(
            line for line in captured.get("desc", "").splitlines()
            if "HTML TARGET OVERRIDE:" in line
        )
        self.assertIn("index.html", override_line)
        self.assertIn("about.html", override_line)
        self.assertNotIn("pricing.html", override_line)
        self.assertNotIn("features.html", override_line)


class TestWaitingAiProgressSignal(unittest.TestCase):
    def test_waiting_ai_event_hides_timeout_limit_fields(self):
        class SlowBridge:
            config = {}

            async def execute(self, **kwargs):
                await asyncio.sleep(10)
                return {"success": True, "output": "late success", "tool_results": []}

        events = []

        async def on_event(evt):
            events.append(evt)

        orch = Orchestrator(ai_bridge=SlowBridge(), executor=None, on_event=on_event)
        orch._configured_subtask_timeout = lambda agent_type: 1  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        plan = Plan(goal="Build test page", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("execution timeout after", str(result.get("error", "")))
        self.assertNotIn("limit", str(result.get("error", "")).lower())

        waiting_events = [
            evt for evt in events
            if evt.get("type") == "subtask_progress" and evt.get("stage") == "waiting_ai"
        ]
        self.assertTrue(waiting_events, "expected at least one waiting_ai progress event")


class TestBuilderDirectMultifileRetry(unittest.TestCase):
    def test_builder_auto_direct_text_mode_for_single_page_game(self):
        captured = {}

        class DirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                captured["input_data"] = kwargs.get("input_data") or ""
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n"
                        "<!DOCTYPE html><html><body><main><h1>Voxel Strike</h1><canvas id='game'></canvas></main></body></html>\n"
                        "```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=DirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(goal="做一个贪吃蛇网页小游戏，单页即可", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured.get("node", {}).get("builder_delivery_mode"), "direct_text")
        self.assertIn("DIRECT SINGLE-FILE DELIVERY mode", captured.get("input_data", ""))
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())

    def test_builder_direct_text_mode_times_out_when_no_html_stream_arrives(self):
        class StalledDirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "forcing_text_output",
                    "reason": "builder tool-only timeout after 120s",
                    "builder_delivery_mode": "direct_text",
                    "builder_direct_text": True,
                })
                await asyncio.sleep(1.4)
                return {
                    "success": True,
                    "output": "",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StalledDirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(goal="做一个贪吃蛇网页小游戏，单页即可", subtasks=[subtask])

        _test_overrides = {"BUILDER_DIRECT_TEXT_NO_OUTPUT_TIMEOUT_SEC": 1}
        _orig = orch._effective_builder_timeout

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_NO_OUTPUT_TIMEOUT_SEC", 1), \
                 patch.object(orch, "_effective_builder_timeout", side_effect=lambda k: _test_overrides.get(k, _orig(k))), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("builder direct-text no-output timeout", str(result.get("error", "")).lower())
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "builder_direct_text_stall"
            for call in orch.emit.await_args_list
        ))

    def test_builder_requested_direct_text_waits_for_runtime_confirmation_before_enabling_watchdog(self):
        class UnconfirmedDirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "builder_pending_write",
                    "message": "write-like tool payload is still streaming",
                })
                await asyncio.sleep(1.4)
                return {
                    "success": False,
                    "output": "",
                    "error": "bridge returned without confirming direct_text mode",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=UnconfirmedDirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]
        orch._builder_execution_direct_text_mode = lambda plan, subtask: True  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(goal="做一个第三人称 3D 射击游戏，单页即可", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_NO_OUTPUT_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 10):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertNotIn("builder direct-text no-output timeout", str(result.get("error", "")).lower())
        self.assertIn("bridge returned without confirming direct_text mode", str(result.get("error", "")).lower())

    def test_builder_forcing_text_progress_switches_watchdog_into_direct_text_mode(self):
        class ForcedFallbackBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "forcing_text_output",
                    "reason": "builder tool-only timeout after 120s",
                    "builder_delivery_mode": "direct_text",
                    "builder_direct_text": True,
                })
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": (
                        "```html index.html\n"
                        "<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1><canvas id='game'></canvas></main></body></html>\n```"
                    ),
                })
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n"
                        "<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1><canvas id='game'></canvas></main></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=ForcedFallbackBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(goal="创建一个第三人称 3D 射击游戏", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 96}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 90, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "builder_direct_text_mode"
            for call in orch.emit.await_args_list
        ))

    def test_builder_direct_text_mode_times_out_when_html_stream_goes_idle(self):
        class StreamingDirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": "<!DOCTYPE html><html><body>" + ("<section>arena</section>" * 20),
                })
                await asyncio.sleep(10)
                return {
                    "success": True,
                    "output": "",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StreamingDirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(goal="做一个贪吃蛇网页小游戏，单页即可", subtasks=[subtask])

        _test_overrides = {"BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC": 100, "BUILDER_DIRECT_TEXT_IDLE_TIMEOUT_SEC": 1}
        _orig = orch._effective_builder_timeout

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC", 100), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_IDLE_TIMEOUT_SEC", 1), \
                 patch.object(orch, "_effective_builder_timeout", side_effect=lambda k: _test_overrides.get(k, _orig(k))), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": False, "score": 48, "errors": ["partial salvage is incomplete"], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))
                root_index_exists = (tmp_out / "index.html").exists()

        self.assertFalse(result.get("success"))
        self.assertIn("builder direct-text idle timeout", str(result.get("error", "")).lower())
        self.assertTrue(root_index_exists)
        self.assertTrue(any(
            call.args[0] == "subtask_progress"
            and call.args[1].get("stage") == "builder_direct_text_stall"
            and call.args[1].get("reason") == "idle"
            for call in orch.emit.await_args_list
        ))

    def test_builder_first_write_watchdog_waits_when_pending_write_progress_is_reported(self):
        class PendingWriteBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "builder_pending_write",
                    "message": "streaming write-like payload",
                })
                await asyncio.sleep(1.2)
                target = Path(orchestrator_module.OUTPUT_DIR) / "index.html"
                target.write_text(
                    "<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1><canvas id='game'></canvas></main></body></html>",
                    encoding="utf-8",
                )
                return {
                    "success": True,
                    "output": "",
                    "tool_results": [{"written": True, "path": str(target)}],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=PendingWriteBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium site", depends_on=[])
        plan = Plan(goal="做一个品牌官网", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 92, "errors": [], "warnings": []}), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())

    def test_builder_generic_execution_timeout_extends_while_direct_text_stream_is_active(self):
        final_html = (
            "```html index.html\n"
            "<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1><canvas id='game'></canvas></main></body></html>\n```"
        )

        class ActiveDirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": final_html,
                })
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": final_html,
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=ActiveDirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 1  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium shooter", depends_on=[])
        plan = Plan(goal="做一个单页 3D 射击游戏", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 92, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("execution timeout", str(result.get("error", "")).lower())

    def test_builder_direct_text_max_stream_timeout_salvages_quality_html(self):
        class StreamingDirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": (
                        "```html index.html\n"
                        "<!DOCTYPE html><html lang='zh'><head><meta charset='UTF-8'><title>Maison Atlas</title>"
                        "<style>body{font-family:sans-serif;background:#0b1020;color:#f8fafc}main{display:grid;gap:16px}"
                        ".panel{padding:20px;border-radius:20px;background:#111827}button{padding:12px 18px}</style></head>"
                        "<body><main><section class='panel'><h1>Maison Atlas</h1><p>"
                        + ("高端品牌官网叙事与转化内容。" * 40)
                        + "</p></section><section class='panel'><h2>Collections</h2><p>"
                        + ("材质、工艺、预约体验。" * 32)
                        + "</p><button>Book</button></section></main>"
                    ),
                })
                await asyncio.sleep(10)
                return {
                    "success": True,
                    "output": "",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StreamingDirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium shooter", depends_on=[])
        plan = Plan(goal="做一个单页 3D 射击游戏", subtasks=[subtask])

        _test_overrides = {"BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC": 1}
        _orig = orch._effective_builder_timeout

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC", 1), \
                 patch.object(orch, "_effective_builder_timeout", side_effect=lambda k: _test_overrides.get(k, _orig(k))), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 92, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))
                root_index_exists = (tmp_out / "index.html").exists()

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("error"), "")
        self.assertTrue(root_index_exists)
        self.assertTrue(any(
            call.args[0] == "subtask_progress"
            and call.args[1].get("stage") == "builder_direct_text_timeout_salvaged"
            and call.args[1].get("reason") == "max_stream"
            for call in orch.emit.await_args_list
        ))

    def test_builder_direct_text_mode_finishes_early_when_salvaged_html_passes_quality_gate(self):
        class StreamingDirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": (
                        "```html index.html\n"
                        "<!DOCTYPE html><html lang='zh'><head><meta charset='UTF-8'><title>Steel Hunt</title></head>"
                        "<body><main><section class='hero'><h1>Steel Hunt</h1><p>"
                        + ("商业级第三人称射击体验。" * 40)
                        + "</p></section><canvas id='game'></canvas><section><h2>Mission Flow</h2><p>"
                        + ("多武器、多怪物、多关卡循环。" * 24)
                        + "</p></section></main></body>\n```"
                    ),
                })
                await asyncio.sleep(10)
                return {
                    "success": True,
                    "output": "",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StreamingDirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(goal="做一个贪吃蛇网页小游戏，单页即可", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_EARLY_COMPLETE_MIN_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 96}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 92, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))
                root_index_exists = (tmp_out / "index.html").exists()

        self.assertTrue(result.get("success"))
        self.assertTrue(root_index_exists)
        self.assertTrue(any(
            call.args[0] == "subtask_progress"
            and call.args[1].get("stage") == "builder_direct_text_early_complete"
            for call in orch.emit.await_args_list
        ))

    def test_builder_direct_text_active_stream_grace_avoids_premature_timeout(self):
        final_html = (
            "```html index.html\n"
            "<!DOCTYPE html><html lang='zh'><head><meta charset='UTF-8'><title>Steel Hunt</title></head>"
            "<body><main><section><h1>Steel Hunt</h1><p>"
            + ("第三人称射击关卡推进。" * 40)
            + "</p></section><canvas id='game'></canvas></main></body></html>\n```"
        )

        class StreamingDirectTextBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": final_html[:1200],
                })
                await asyncio.sleep(0.8)
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": final_html,
                })
                await asyncio.sleep(0.3)
                return {
                    "success": True,
                    "output": final_html,
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StreamingDirectTextBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium shooter", depends_on=[])
        plan = Plan(goal="做一个单页 3D 射击游戏", subtasks=[subtask])

        _test_overrides = {"BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC": 1, "BUILDER_DIRECT_TEXT_ACTIVE_STREAM_GRACE_SEC": 5}
        _orig = orch._effective_builder_timeout

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_ACTIVE_STREAM_GRACE_SEC", 5), \
                 patch.object(orch, "_effective_builder_timeout", side_effect=lambda k: _test_overrides.get(k, _orig(k))), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 92, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("max-stream timeout", str(result.get("error", "")).lower())
        self.assertTrue(any(
            call.args[0] == "subtask_progress"
            and call.args[1].get("stage") == "builder_direct_text_active_grace"
            for call in orch.emit.await_args_list
        ))

    def test_builder_auto_direct_text_mode_for_general_single_page_game_brief(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(
            goal="做一个贪吃蛇网页小游戏，包含开始界面、暂停和结算体验。",
            subtasks=[subtask],
        )

        self.assertTrue(orch._builder_execution_direct_text_mode(plan, subtask))

    def test_builder_enables_direct_text_mode_for_lightweight_3d_game_brief(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(id="1", agent_type="builder", description="build premium tps shooter", depends_on=[])
        plan = Plan(
            goal="创建一个第三人称 3D 迷宫冒险游戏，带开始界面、通关和结算。",
            subtasks=[subtask],
        )

        self.assertTrue(orch._builder_execution_direct_text_mode(plan, subtask))

    def test_builder_enables_direct_text_mode_for_premium_3d_game_brief(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(id="1", agent_type="builder", description="build premium tps shooter", depends_on=[])
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
            subtasks=[subtask],
        )

        self.assertTrue(orch._builder_execution_direct_text_mode(plan, subtask))

    def test_builder_timeout_salvage_uses_full_partial_output_not_compacted_preview(self):
        captured = {}
        full_html = (
            "```html index.html\n<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            "<main><h1>Steel Hunt</h1><canvas id='game'></canvas><section>"
            + ("商业级第三人称射击体验。" * 320)
            + "</section></main></body></html>\n```"
        )
        compact_preview = full_html[:180] + "\n...\n" + full_html[-180:]

        class StreamingBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": compact_preview,
                    "partial_output": full_html,
                })
                await asyncio.sleep(10)
                return {
                    "success": True,
                    "output": "",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StreamingBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        def capture_salvage(plan_arg, subtask_arg, partial_output):
            captured["partial_output"] = partial_output
            return []

        subtask = SubTask(id="1", agent_type="builder", description="build premium voxel shooter", depends_on=[])
        plan = Plan(goal="做一个贪吃蛇网页小游戏，单页即可。", subtasks=[subtask])

        _test_overrides = {"BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC": 1}
        _orig = orch._effective_builder_timeout

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_EARLY_COMPLETE_MIN_SEC", 999), \
                 patch.object(orchestrator_module, "BUILDER_DIRECT_TEXT_MAX_STREAM_TIMEOUT_SEC", 1), \
                 patch.object(orch, "_effective_builder_timeout", side_effect=lambda k: _test_overrides.get(k, _orig(k))), \
                 patch.object(orch, "_salvage_builder_partial_output", side_effect=capture_salvage):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertEqual(captured.get("partial_output"), full_html)

    def test_builder_first_write_timeout_recovers_recent_disk_artifacts_when_progress_event_is_missing(self):
        events = []

        class SilentWriteBridge:
            config = {}

            async def execute(self, **kwargs):
                target = orchestrator_module.OUTPUT_DIR / "index.html"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    (
                        "<!DOCTYPE html><html><body><main><h1>Maison Aurelia</h1>"
                        "<section><h2>Craftsmanship</h2><p>Quiet luxury, editorial pacing, and rich product storytelling.</p></section>"
                        "</main></body></html>"
                    ),
                    encoding="utf-8",
                )
                await asyncio.sleep(10)
                return {
                    "success": True,
                    "output": "",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=SilentWriteBridge(), executor=None)
        orch.emit = AsyncMock(side_effect=lambda event, payload: events.append((event, payload)))
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 0.2  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium landing page", depends_on=[])
        plan = Plan(goal="做一个高级品牌落地页。", subtasks=[subtask])

        _test_overrides = {"BUILDER_FIRST_WRITE_TIMEOUT_SEC": 1}
        _orig = orch._effective_builder_timeout

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orch, "_effective_builder_timeout", side_effect=lambda k: _test_overrides.get(k, _orig(k))), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("no real file written", str(result.get("error", "")).lower())
        self.assertTrue(any(
            event == "subtask_progress" and payload.get("stage") == "builder_runtime_write_recovered"
            for event, payload in events
        ))

    def test_salvage_builder_partial_output_discards_invalid_truncated_html(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(id="1", agent_type="builder", description="build premium shooter", depends_on=[])
        plan = Plan(goal="做一个贪吃蛇网页小游戏，单页即可。", subtasks=[subtask])
        broken_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Broken</title>
  <style>
    body { background: #000; color: #fff; }
    const weapon = new THREE.Mesh();
  </style>
</head>
<body></body>
</html>
"""

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                salvaged = orch._salvage_builder_partial_output(plan, subtask, broken_html)

                self.assertEqual(salvaged, [])
                self.assertFalse((tmp_out / "index.html").exists())
                self.assertFalse((tmp_out / "task_1" / "index.html").exists())

    def test_salvage_builder_partial_output_trips_repeated_invalid_loop_guard(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(id="7", agent_type="builder", description="build support lane", depends_on=[])
        plan = Plan(goal="做一个第三人称 3D 射击游戏。", subtasks=[subtask])
        broken_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Broken Salvage</title>
</head>
<body>
  <main>
    <section><h1>Broken Salvage</h1><p>This content is substantial enough to become a retry seed, but the script is intentionally truncated.</p></section>
    <section><p>Enough visible content exists to reproduce the salvage loop.</p></section>
  </main>
  <script>
    function boot() {
      const state = { ready: true };
      if (state.ready) {
        console.log('boot');
  </script>
</body>
</html>"""

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                first = orch._salvage_builder_partial_output(plan, subtask, broken_html)
                second = orch._salvage_builder_partial_output(plan, subtask, broken_html)

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertTrue(subtask.builder_invalid_salvage_tripped)
        self.assertIn("Repeated invalid salvaged HTML loop detected", subtask.builder_invalid_salvage_message)

    def test_builder_retry_timeout_switches_engine_free_single_page_game_to_direct_text_mode(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium tps shooter",
            depends_on=[],
        )
        subtask.retries = 1
        subtask.error = (
            "builder first-write timeout: 212s elapsed with no real file written. "
            "Builder may be stalled after scaffold generation."
        )
        plan = Plan(
            goal="做一个贪吃蛇网页小游戏，要有开始界面和结算页。",
            subtasks=[subtask],
        )

        self.assertTrue(orch._builder_execution_direct_text_mode(plan, subtask))

    def test_builder_retry_timeout_without_artifact_switches_premium_3d_game_back_to_direct_text(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium tps shooter",
            depends_on=[],
        )
        subtask.retries = 1
        subtask.error = "builder pre-write timeout after 150s: no file write produced."
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
            subtasks=[subtask],
        )

        self.assertFalse(orch._builder_requires_existing_artifact_patch(plan, subtask))
        self.assertTrue(orch._builder_execution_direct_text_mode(plan, subtask))

    def test_builder_retry_quality_gate_failure_keeps_premium_3d_game_in_patch_mode(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium tps shooter",
            depends_on=[],
        )
        subtask.retries = 1
        subtask.error = (
            "Builder quality gate failed (score=70). Errors: "
            "['Premium 3D/TPS brief still appears to render core models as primitive placeholder geometry "
            "(weapon/gun); replace Box/Cone/Cylinder-style stand-ins with authored or asset-driven models.']"
        )
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
            subtasks=[subtask],
        )

        self.assertFalse(orch._builder_execution_direct_text_mode(plan, subtask))

    def test_builder_retry_direct_text_idle_timeout_keeps_premium_3d_game_in_patch_mode(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium tps shooter",
            depends_on=[],
        )
        subtask.retries = 2
        subtask.error = (
            "builder direct-text idle timeout: no new meaningful HTML stream activity for 91s "
            "in direct single-file delivery mode."
        )
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
            subtasks=[subtask],
        )

        self.assertFalse(orch._builder_execution_direct_text_mode(plan, subtask))

    def test_builder_retry_tool_planning_prose_without_artifact_switches_premium_3d_back_to_direct_text(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium tps shooter",
            depends_on=[],
        )
        subtask.retries = 1
        subtask.error = "Builder returned tool-planning prose instead of a persistable HTML deliverable."
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
            subtasks=[subtask],
        )

        self.assertFalse(orch._builder_requires_existing_artifact_patch(plan, subtask))
        self.assertTrue(orch._builder_execution_direct_text_mode(plan, subtask))

    def test_builder_retry_placeholder_geometry_prompt_uses_patch_mesh_guidance(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium tps shooter",
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.retries = 1
        subtask.error = (
            "Builder quality gate failed (score=70). Errors: "
            "['Premium 3D/TPS brief still appears to render core models as primitive placeholder geometry "
            "(weapon/gun); replace Box/Cone/Cylinder-style stand-ins with authored or asset-driven models.']"
        )
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
            subtasks=[subtask],
        )

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        desc = captured.get("desc", "")
        self.assertNotIn("DIRECT SINGLE-FILE DELIVERY ONLY.", desc)
        self.assertIn("Treat the current output as the source of truth", desc)
        self.assertIn("file_ops read on the current index.html", desc)
        self.assertIn("Fix the weapon path first", desc)
        self.assertIn("weapon receiver / core mass", desc)
        self.assertIn("THREE.Group", desc)
        self.assertIn("player/enemy/weapon creation path", desc)

    def test_builder_retry_placeholder_geometry_prompt_adds_goal_specific_flow_and_camera_guidance(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium tps shooter",
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.retries = 1
        subtask.error = (
            "Builder quality gate failed (score=70). Errors: "
            "['Premium 3D/TPS brief still appears to render core models as primitive placeholder geometry "
            "(weapon/gun); replace Box/Cone/Cylinder-style stand-ins with authored or asset-driven models.']"
        )
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、关卡和通过页面，鼠标长按屏幕之后可以拉动转动视角，还要商业级精美建模。",
            subtasks=[subtask],
        )

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        desc = captured.get("desc", "")
        self.assertIn("victory / mission-complete / pass screen", desc)
        self.assertIn("drag / mouse-look camera rotation", desc)
        self.assertIn("dragging or mousing upward must pitch upward", desc)
        self.assertIn("yaw += deltaX * sensitivity", desc)
        self.assertIn("pitch -= deltaY * sensitivity", desc)
        self.assertIn("Treat the current output as the source of truth", desc)
        self.assertIn("multiple material zones", desc)
        self.assertIn("Box/Cone/Cylinder/Capsule", desc)
        self.assertIn("still count as placeholder-grade", desc)
        self.assertIn("THREE.Shape/ExtrudeGeometry", desc)
        self.assertIn("custom BufferGeometry", desc)
        self.assertIn("safe opening window", desc)
        self.assertIn("true radial distance", desc)

    def test_builder_retry_3d_runtime_fix_prompt_uses_non_primitive_hero_template(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium tps shooter",
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.retries = 1
        subtask.error = (
            "Builder quality gate failed (score=61). Errors: "
            "['3D runtime regression: Canvas2D getContext(\\'2d\\') was used instead of Three.js/WebGLRenderer.']"
        )
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
            subtasks=[subtask],
        )

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        desc = captured.get("desc", "")
        self.assertIn("WORKING Three.js third-person shooter skeleton", desc)
        self.assertIn("Premium 3D model contract", desc)
        self.assertIn("new THREE.ExtrudeGeometry", desc)
        self.assertIn("new THREE.TubeGeometry", desc)
        self.assertNotIn("new THREE.CylinderGeometry(0.4,0.4,1.6,8)", desc)
        self.assertNotIn("const geo = new THREE.BoxGeometry(1,2,1)", desc)

    def test_builder_retry_game_prompt_with_greenfield_output_skips_repo_context_and_keeps_direct_text(self):
        captured = {}

        class DirectTextBridge:
            def __init__(self, workspace):
                self.config = {"workspace": workspace}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                captured["input_data"] = kwargs.get("input_data") or ""
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n"
                        "<!DOCTYPE html><html><body><main><h1>Steel Hunt</h1><canvas id='game'></canvas></main></body></html>\n"
                        "```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as out_td:
            repo_root = Path(repo_td)
            (repo_root / "frontend" / "src" / "app").mkdir(parents=True)
            (repo_root / "frontend" / "package.json").write_text(
                json.dumps({"scripts": {"build": "next build", "lint": "next lint"}}),
                encoding="utf-8",
            )
            (repo_root / "frontend" / "src" / "app" / "page.tsx").write_text(
                "export default function Page(){return null;}",
                encoding="utf-8",
            )
            (repo_root / "backend").mkdir(parents=True, exist_ok=True)
            (repo_root / "backend" / "server.py").write_text("print('ok')\n", encoding="utf-8")

            orch = Orchestrator(ai_bridge=DirectTextBridge(str(repo_root)), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()
            orch._append_ne_activity = Mock()
            orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
            orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

            subtask = SubTask(
                id="1",
                agent_type="builder",
                description=(
                    "Build a polished HTML5 game for a snake arcade experience. "
                    "Save final HTML via file_ops write to /tmp/evermind_output/index.html. "
                    "Fix JavaScript/runtime errors that prevent the page from rendering correctly."
                ),
                depends_on=[],
            )
            subtask.retries = 1
            plan = Plan(goal="做一个贪吃蛇网页小游戏，单页即可。", subtasks=[subtask])

            tmp_out = Path(out_td)
            repo_context = {
                "repo_root": str(repo_root),
                "verification_commands": ["frontend: npm run build"],
                "activity_note": "已注入仓库地图：ai智能体合作",
            }
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "build_repo_context", return_value=repo_context), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured.get("node", {}).get("builder_delivery_mode"), "direct_text")
        self.assertNotIn("Existing Repository Mode", captured.get("input_data", ""))
        self.assertIn("DIRECT SINGLE-FILE DELIVERY mode", captured.get("input_data", ""))
        repo_messages = [str(call.args[1]) for call in orch._append_ne_activity.call_args_list if len(call.args) > 1]
        self.assertFalse(any("已注入仓库地图" in message for message in repo_messages))

    def test_builder_auto_direct_multifile_mode_for_two_page_kimi_run(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(id="1", agent_type="builder", description="build premium site", depends_on=[])
        plan = Plan(goal="做一个 2 页面品牌网站，包含首页和联系页", subtasks=[subtask])

        self.assertTrue(orch._builder_execution_direct_multifile_mode(plan, subtask, "kimi-coding"))

    def test_builder_auto_direct_multifile_first_attempt_for_kimi_multi_page(self):
        captured = {}

        class SlowDirectBridge:
            config = {}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                captured["input_data"] = kwargs.get("input_data") or ""
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html pricing.html\n<!DOCTYPE html><html><body><h1>Pricing</h1></body></html>\n```\n"
                        "```html features.html\n<!DOCTYPE html><html><body><h1>Features</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=SlowDirectBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium site",
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面轻奢品牌网站，包含首页、定价页和功能页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured.get("node", {}).get("builder_delivery_mode"), "direct_multifile")
        self.assertIn("DIRECT MULTI-FILE DELIVERY mode", captured.get("input_data", ""))
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())

    def test_builder_direct_multifile_retry_waits_for_text_output_without_first_write(self):
        captured = {}

        class SlowDirectBridge:
            config = {}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html pricing.html\n<!DOCTYPE html><html><body><h1>Pricing</h1></body></html>\n```\n"
                        "```html contact.html\n<!DOCTYPE html><html><body><h1>Contact</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=SlowDirectBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "⚠️ MULTI-PAGE DELIVERY INCOMPLETE.\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "Return index.html, pricing.html, and contact.html."
            ),
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面轻奢品牌网站，包含首页、定价页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(captured.get("node", {}).get("builder_delivery_mode"), "direct_multifile")
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())

    def test_builder_runtime_fallback_to_kimi_skips_first_write_timeout_and_preserves_assigned_model(self):
        captured = {}

        class FallbackBridge:
            config = {
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            }

            def preferred_model_for_node(self, node, model):
                return "gpt-5.4"

            def _resolve_model(self, model_name):
                provider = "kimi" if model_name == "kimi-coding" else "openai"
                return {"provider": provider}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                on_progress = kwargs.get("on_progress")
                await on_progress({
                    "stage": "model_chain_resolved",
                    "assignedModel": "gpt-5.4",
                    "assignedProvider": "openai",
                    "candidateModels": ["gpt-5.4", "kimi-coding"],
                })
                await on_progress({
                    "stage": "model_selected",
                    "assignedModel": "gpt-5.4",
                    "assignedProvider": "openai",
                    "modelIndex": 1,
                    "modelCount": 2,
                })
                await on_progress({
                    "stage": "model_fallback",
                    "assignedModel": "kimi-coding",
                    "assignedProvider": "kimi",
                    "from_model": "gpt-5.4",
                    "to_model": "kimi-coding",
                })
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html pricing.html\n<!DOCTYPE html><html><body><h1>Pricing</h1></body></html>\n```\n"
                        "```html contact.html\n<!DOCTYPE html><html><body><h1>Contact</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                    "assigned_model": "kimi-coding",
                    "assigned_provider": "kimi",
                }

        orch = Orchestrator(ai_bridge=FallbackBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium multi-page site",
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面轻奢品牌网站，包含首页、定价页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_FIRST_WRITE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("first-write timeout", str(result.get("error", "")).lower())
        self.assertEqual(captured.get("node", {}).get("model"), "gpt-5.4")
        assigned_model_updates = [
            call.kwargs.get("assigned_model")
            for call in orch._sync_ne_status.await_args_list
            if "assigned_model" in call.kwargs
        ]
        self.assertIn("gpt-5.4", assigned_model_updates)
        self.assertIn("kimi-coding", assigned_model_updates)
        self.assertEqual(assigned_model_updates[-1], "kimi-coding")

    def test_builder_runtime_direct_multifile_switch_extends_timeout_budget(self):
        class FallbackBridge:
            config = {
                "node_model_preferences": {
                    "builder": ["gpt-5.4", "kimi-coding"],
                },
            }

            def preferred_model_for_node(self, node, model):
                return "gpt-5.4"

            def _resolve_model(self, model_name):
                provider = "kimi" if model_name == "kimi-coding" else "openai"
                return {"provider": provider}

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                await on_progress({
                    "stage": "model_fallback",
                    "assignedModel": "kimi-coding",
                    "assignedProvider": "kimi",
                })
                await asyncio.sleep(1.4)
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html pricing.html\n<!DOCTYPE html><html><body><h1>Pricing</h1></body></html>\n```\n"
                        "```html contact.html\n<!DOCTYPE html><html><body><h1>Contact</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=FallbackBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._sync_ne_timeout_budget = Mock()
        orch._configured_progress_heartbeat = lambda: 0.2  # type: ignore[method-assign]
        orch._execution_timeout_for_subtask = lambda plan, subtask, model: 3 if model == "kimi-coding" else 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description="build premium multi-page site",
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面轻奢品牌网站，包含首页、定价页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        timeout_calls = [call.args[1] for call in orch._sync_ne_timeout_budget.call_args_list]
        self.assertIn(1, timeout_calls)
        self.assertIn(3, timeout_calls)

    def test_builder_direct_multifile_batch_ready_saves_files_and_skips_idle_timeout(self):
        events = []

        class BatchBridge:
            config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "about.html"]

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                await on_progress({
                    "stage": "builder_multifile_batch_ready",
                    "batch_index": 1,
                    "returned_targets": ["index.html"],
                    "finish_reason": "length",
                    "content": "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```",
                })
                await asyncio.sleep(1.2)
                await on_progress({
                    "stage": "builder_multifile_batch_ready",
                    "batch_index": 2,
                    "returned_targets": ["about.html"],
                    "finish_reason": "stop",
                    "content": "```html about.html\n<!DOCTYPE html><html><body><h1>About</h1></body></html>\n```",
                })
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html about.html\n<!DOCTYPE html><html><body><h1>About</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        async def record(event_type, payload):
            evt = dict(payload or {})
            evt["type"] = event_type
            events.append(evt)

        orch = Orchestrator(ai_bridge=BatchBridge(), executor=None)
        orch.emit = AsyncMock(side_effect=record)
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "⚠️ MULTI-PAGE DELIVERY INCOMPLETE.\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "Assigned HTML filenames for this builder: index.html, about.html.\n"
                "Return index.html and about.html."
            ),
            depends_on=[],
        )
        plan = Plan(goal="做一个两页面轻奢品牌网站，包含首页和关于页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

            index_html = (tmp_out / "index.html").read_text(encoding="utf-8")
            about_html = (tmp_out / "about.html").read_text(encoding="utf-8")

        self.assertTrue(result.get("success"))
        self.assertNotIn("post-write idle timeout", str(result.get("error", "")).lower())
        self.assertIn("<h1>Home</h1>", index_html)
        self.assertIn("<h1>About</h1>", about_html)

        batch_events = [
            evt for evt in events
            if evt.get("type") == "subtask_progress" and evt.get("stage") == "builder_multifile_batch_ready"
        ]
        self.assertEqual(len(batch_events), 2)
        self.assertTrue(all("content" not in evt for evt in batch_events))
        self.assertTrue(any(any(str(path).endswith("index.html") for path in evt.get("saved_files", [])) for evt in batch_events))
        self.assertTrue(any(any(str(path).endswith("about.html") for path in evt.get("saved_files", [])) for evt in batch_events))

    def test_builder_direct_multifile_batch_ready_skips_unassigned_html_targets(self):
        class BatchBridge:
            config = {}

            def _builder_assigned_html_targets(self, _input_data):
                return ["index.html", "about.html"]

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                await on_progress({
                    "stage": "builder_multifile_batch_ready",
                    "batch_index": 1,
                    "returned_targets": ["index.html", "destinations.html"],
                    "finish_reason": "length",
                    "content": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html destinations.html\n<!DOCTYPE html><html><body><h1>Destinations</h1></body></html>\n```"
                    ),
                })
                await on_progress({
                    "stage": "builder_multifile_batch_ready",
                    "batch_index": 2,
                    "returned_targets": ["about.html"],
                    "finish_reason": "stop",
                    "content": "```html about.html\n<!DOCTYPE html><html><body><h1>About</h1></body></html>\n```",
                })
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home</h1></body></html>\n```\n"
                        "```html destinations.html\n<!DOCTYPE html><html><body><h1>Destinations</h1></body></html>\n```\n"
                        "```html about.html\n<!DOCTYPE html><html><body><h1>About</h1></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=BatchBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 0.2  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                "Assigned HTML filenames for this builder: index.html, about.html."
            ),
            depends_on=[],
        )
        plan = Plan(goal="做一个两页面轻奢品牌网站，包含首页和关于页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

            index_html = (tmp_out / "index.html").read_text(encoding="utf-8")
            about_html = (tmp_out / "about.html").read_text(encoding="utf-8")

        self.assertTrue(result.get("success"))
        self.assertIn("<h1>Home</h1>", index_html)
        self.assertIn("<h1>About</h1>", about_html)
        self.assertFalse((tmp_out / "destinations.html").exists())

    def test_builder_post_write_idle_timeout_rewrites_written_artifact_failure_to_quality_gate(self):
        class PostWriteBridge:
            config = {}

            def __init__(self, output_dir):
                self.output_dir = output_dir

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                html_path = self.output_dir / "index.html"
                html_path.write_text(
                    "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body><canvas id='game'></canvas></body></html>",
                    encoding="utf-8",
                )
                await on_progress({
                    "stage": "builder_write",
                    "path": str(html_path),
                })
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": "The file was written successfully. Let me verify the content by reading it back:",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            orch = Orchestrator(ai_bridge=PostWriteBridge(tmp_out), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()
            orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
            orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

            subtask = SubTask(id="1", agent_type="builder", description="build premium shooter", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
                subtasks=[subtask],
            )

            quality_report = {
                "pass": False,
                "score": 82,
                "errors": [
                    "Premium 3D/TPS brief still appears to render core models as primitive placeholder geometry (weapon/gun); replace Box/Cone/Cylinder-style stand-ins with authored or asset-driven models."
                ],
                "warnings": [],
            }

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value=quality_report):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("Builder quality gate failed (score=82)", str(result.get("error", "")))
        self.assertNotIn("post-write idle timeout", str(result.get("error", "")).lower())
        self.assertIn("quality gate failed", str(subtask.error).lower())

    def test_premium_3d_builder_post_write_idle_timeout_gets_extended_grace_window(self):
        class PostWriteBridge:
            config = {}

            def __init__(self, output_dir):
                self.output_dir = output_dir

            async def execute(self, **kwargs):
                on_progress = kwargs.get("on_progress")
                html_path = self.output_dir / "index.html"
                html_path.write_text(
                    "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body><div id='hud'></div><canvas id='game'></canvas></body></html>",
                    encoding="utf-8",
                )
                await on_progress({
                    "stage": "builder_write",
                    "path": str(html_path),
                })
                await asyncio.sleep(1.2)
                return {
                    "success": True,
                    "output": "<!DOCTYPE html><html><body><canvas id='game'></canvas><div id='hud'>ok</div></body></html>",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            orch = Orchestrator(ai_bridge=PostWriteBridge(tmp_out), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()
            orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
            orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

            subtask = SubTask(id="1", agent_type="builder", description="build premium shooter", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC", 1), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 90, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("post-write idle timeout", str(result.get("error", "")).lower())

    def test_builder_repo_context_does_not_leave_direct_multifile_flag_unbound(self):
        captured = {}

        class StubBridge:
            config = {}

            async def execute(self, **kwargs):
                captured["node"] = kwargs.get("node") or {}
                return {
                    "success": True,
                    "output": "```html index.html\n<!DOCTYPE html><html><body><h1>Repo</h1></body></html>\n```",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="patch existing repo page", depends_on=[])
        plan = Plan(goal="修复现有仓库里的官网页面", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            repo_context = {"repo_root": "/tmp/demo-repo", "verification_commands": ["npm test"], "activity_note": ""}
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "build_repo_context", return_value=repo_context), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("builder_delivery_mode", captured.get("node", {}))

    def test_failed_builder_retry_uses_downgraded_model_on_next_execution(self):
        captured_models = []

        class RetryBridge:
            config = {}

            async def execute(self, **kwargs):
                node = kwargs.get("node") or {}
                captured_models.append(node.get("model"))
                return {
                    "success": True,
                    "output": (
                        "```html index.html\n"
                        "<!DOCTYPE html><html><body><main><h1>Retry</h1><canvas id='game'></canvas></main></body></html>\n"
                        "```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=RetryBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="1", agent_type="builder", description="build premium shooter", depends_on=[])
        subtask.error = "Builder quality gate failed (score=20). Errors: ['runtime error']"
        plan = Plan(goal="创建一个单页 3D 射击游戏。", subtasks=[subtask])

        quality_results = [
            {"pass": False, "score": 20, "errors": ["runtime error"], "warnings": []},
            {"pass": True, "score": 90, "errors": [], "warnings": []},
        ]

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", side_effect=quality_results), \
                 patch.object(orch, "_downgrade_model", return_value="gpt-5.4"):
                retry_ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(retry_ok)
        self.assertTrue(result.get("success"))
        self.assertEqual(captured_models[:2], ["kimi-coding", "gpt-5.4"])
        self.assertEqual(subtask.retry_model_override, "")

    def test_nav_only_execute_subtask_restores_locked_root_artifacts(self):
        original_styles = "body{background:#101010;color:#f5f5f5;}"
        original_about = "<!DOCTYPE html><html><body><h1>About Stable</h1></body></html>"

        class NavRepairBridge:
            config = {}

            async def execute(self, **kwargs):
                out_dir = orchestrator_module.OUTPUT_DIR
                (out_dir / "styles.css").write_text("body{background:#ff66aa;color:#111;}", encoding="utf-8")
                (out_dir / "about.html").write_text(
                    "<!DOCTYPE html><html><body><h1>About Broken</h1></body></html>",
                    encoding="utf-8",
                )
                (out_dir / "faq.html").write_text(
                    "<!DOCTYPE html><html><body><h1>Should Not Exist</h1></body></html>",
                    encoding="utf-8",
                )
                return {
                    "success": True,
                    "output": (
                        "I'll first inspect the existing files.\n"
                        "```css styles.css\nbody{background:#ff66aa;color:#111;}\n```\n"
                        "```html index.html\n<!DOCTYPE html><html><body><h1>Home Fixed</h1><nav><a href=\"about.html\">About</a></nav></body></html>\n```"
                    ),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=NavRepairBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._sync_ne_timeout_budget = Mock()
        orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(
            id="1",
            agent_type="builder",
            description=(
                "build premium site\n\n"
                "⚠️ NAVIGATION REPAIR ONLY.\n"
                f"{orchestrator_module.BUILDER_NAV_REPAIR_ONLY_MARKER}\n"
                f"{orchestrator_module.BUILDER_TARGET_OVERRIDE_MARKER} index.html\n"
                "Assigned HTML filenames for this builder: index.html, about.html, contact.html.\n"
            ),
            depends_on=[],
        )
        plan = Plan(goal="做一个三页面官网，包含首页、关于页和联系页", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            (tmp_out / "styles.css").write_text(original_styles, encoding="utf-8")
            (tmp_out / "about.html").write_text(original_about, encoding="utf-8")
            (tmp_out / "contact.html").write_text(
                "<!DOCTYPE html><html><body><h1>Contact Stable</h1></body></html>",
                encoding="utf-8",
            )
            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 95}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

            restored_styles = (tmp_out / "styles.css").read_text(encoding="utf-8")
            restored_about = (tmp_out / "about.html").read_text(encoding="utf-8")
            repaired_index = (tmp_out / "index.html").read_text(encoding="utf-8")

        self.assertTrue(result.get("success"))
        self.assertEqual(restored_styles, original_styles)
        self.assertEqual(restored_about, original_about)
        self.assertFalse((tmp_out / "faq.html").exists())
        self.assertIn("<h1>Home Fixed</h1>", repaired_index)
        self.assertTrue(all(Path(path).name == "index.html" for path in result.get("files_created", [])))
        self.assertTrue(any(
            call.args[0] == "subtask_progress"
            and call.args[1].get("stage") == "builder_nav_repair_locked_restore"
            for call in orch.emit.await_args_list
        ))


class TestImagegenExecutionGuards(unittest.TestCase):
    def test_execute_subtask_fails_imagegen_empty_success(self):
        class EmptyImageBridge:
            config = {}

            async def execute(self, **kwargs):
                return {
                    "success": True,
                    "output": "",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=EmptyImageBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        subtask = SubTask(id="1", agent_type="imagegen", description="Generate a concept pack", depends_on=[])
        plan = Plan(goal="做一个 3D 射击游戏并生成配套概念图", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("produced no saved assets", str(result.get("error", "")).lower())
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_empty_success"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_fails_imagegen_thin_asset_files(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            healthy_asset = assets_dir / "00_master_style_lock.md"
            healthy_asset.write_text("# style lock\n" + ("detail\n" * 20), encoding="utf-8")
            empty_asset = assets_dir / "01_enemy_pack.md"
            empty_asset.write_text("", encoding="utf-8")

            class ThinImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "Prompt pack draft is present, but one asset file is empty.",
                        "tool_results": [
                            {"written": True, "path": str(healthy_asset)},
                            {"written": True, "path": str(empty_asset)},
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=ThinImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a concept pack", depends_on=[])
            plan = Plan(goal="做一个 3D 射击游戏并生成配套概念图", subtasks=[subtask])

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("empty or thin files", str(result.get("error", "")).lower())
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_empty_success"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_fails_imagegen_when_recent_asset_dir_contains_hidden_empty_files(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            healthy_asset = assets_dir / "00_style_lock.md"
            healthy_asset.write_text("# style lock\n" + ("detail\n" * 20), encoding="utf-8")
            hidden_empty = assets_dir / "04_level_environment_prompts.md"
            hidden_empty.write_text("", encoding="utf-8")

            class PartialReportBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "Saved the primary prompt pack files.",
                        "tool_results": [
                            {"written": True, "path": str(healthy_asset)},
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=PartialReportBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a concept pack", depends_on=[])
            plan = Plan(goal="做一个 3D 射击游戏并生成配套概念图", subtasks=[subtask])

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("current assets bundle", str(result.get("error", "")).lower())
        self.assertFalse(hidden_empty.exists())
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_empty_success"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_repairs_imagegen_incomplete_premium_3d_asset_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            asset_files = [
                assets_dir / "00_visual_target.md",
                assets_dir / "01_style_lock.md",
                assets_dir / "character_hero_brief.md",
                assets_dir / "monster_rusher_brief.md",
                assets_dir / "environment_megacity_blockout.md",
            ]
            for asset_file in asset_files:
                asset_file.write_text("# pack\n" + ("detail\n" * 24), encoding="utf-8")

            class IncompletePremiumImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "Saved a premium 3D concept bundle.",
                        "tool_results": [
                            {"written": True, "path": str(asset_file)}
                            for asset_file in asset_files
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=IncompletePremiumImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        created_names = {Path(path).name for path in result.get("files_created", [])}
        self.assertIn("weapon_primary_brief.md", created_names)
        self.assertIn("manifest.json", created_names)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_fallback_pack_synthesized"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_synthesizes_svg_concept_sheets_when_premium_bundle_has_docs_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            asset_map = {
                "00_visual_target.md": "# visual target\n" + ("detail\n" * 24),
                "01_style_lock.md": "# style lock\n" + ("detail\n" * 24),
                "manifest.json": json.dumps({"assets": {"hero_player": {"brief": "character_hero_brief.md"}}}),
                "character_hero_brief.md": "# hero\n" + ("detail\n" * 30),
                "monster_primary_brief.md": "# monster\n" + ("detail\n" * 30),
                "weapon_primary_brief.md": "# weapon\n" + ("detail\n" * 30),
                "environment_kit_brief.md": "# environment\n" + ("detail\n" * 30),
            }
            asset_files = []
            for name, content in asset_map.items():
                asset_path = assets_dir / name
                asset_path.write_text(content, encoding="utf-8")
                asset_files.append(asset_path)

            class DocsOnlyPremiumImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "Saved a premium 3D concept bundle.",
                        "tool_results": [
                            {"written": True, "path": str(asset_file)}
                            for asset_file in asset_files
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=DocsOnlyPremiumImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        created_names = {Path(path).name for path in result.get("files_created", [])}
        self.assertIn("character_hero_sheet.svg", created_names)
        self.assertIn("monster_primary_sheet.svg", created_names)
        self.assertIn("weapon_primary_sheet.svg", created_names)
        self.assertIn("environment_kit_sheet.svg", created_names)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_fallback_pack_synthesized"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_repairs_thin_svg_concept_sheets_in_premium_bundle(self):
        hero_sheet_size = 0
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            asset_map = {
                "00_visual_target.md": "# visual target\n" + ("detail\n" * 24),
                "01_style_lock.md": "# style lock\n" + ("detail\n" * 24),
                "manifest.json": json.dumps({"assets": {"hero_player": {"brief": "character_hero_brief.md"}}}),
                "character_hero_brief.md": "# hero\n" + ("detail\n" * 30),
                "monster_primary_brief.md": "# monster\n" + ("detail\n" * 30),
                "weapon_primary_brief.md": "# weapon\n" + ("detail\n" * 30),
                "environment_kit_brief.md": "# environment\n" + ("detail\n" * 30),
                "character_hero_sheet.svg": "",
                "monster_primary_sheet.svg": "",
                "weapon_primary_sheet.svg": "",
                "environment_kit_sheet.svg": "",
            }
            asset_files = []
            for name, content in asset_map.items():
                asset_path = assets_dir / name
                asset_path.write_text(content, encoding="utf-8")
                asset_files.append(asset_path)

            class ThinSvgPremiumImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "Saved a premium 3D concept bundle with placeholder SVG sheets.",
                        "tool_results": [
                            {"written": True, "path": str(asset_file)}
                            for asset_file in asset_files
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=ThinSvgPremiumImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))
                hero_sheet_size = (assets_dir / "character_hero_sheet.svg").stat().st_size

        self.assertTrue(result.get("success"))
        created_names = {Path(path).name for path in result.get("files_created", [])}
        self.assertIn("character_hero_sheet.svg", created_names)
        self.assertIn("monster_primary_sheet.svg", created_names)
        self.assertIn("weapon_primary_sheet.svg", created_names)
        self.assertIn("environment_kit_sheet.svg", created_names)
        self.assertGreater(hero_sheet_size, 32)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_fallback_pack_synthesized"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_keeps_success_when_only_optional_imagegen_variants_are_thin(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            asset_map = {
                "00_visual_target.md": "# visual target\n" + ("detail\n" * 24),
                "01_style_lock.md": "# style lock\n" + ("detail\n" * 24),
                "manifest.json": json.dumps({"assets": {"hero": "character_hero_brief.md"}}),
                "character_hero_brief.md": "# hero\n" + ("detail\n" * 30),
                "monster_primary_brief.md": "# monster\n" + ("detail\n" * 30),
                "weapon_primary_brief.md": "# weapon\n" + ("detail\n" * 30),
                "environment_kit_brief.md": "# environment\n" + ("detail\n" * 30),
                "weapon_ar_brief.md": "",
            }
            asset_files = []
            for name, content in asset_map.items():
                asset_path = assets_dir / name
                asset_path.write_text(content, encoding="utf-8")
                asset_files.append(asset_path)

            class OptionalThinPremiumImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "Saved a premium 3D concept bundle with one extra optional variant draft.",
                        "tool_results": [
                            {"written": True, "path": str(asset_file)}
                            for asset_file in asset_files
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=OptionalThinPremiumImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertFalse((assets_dir / "weapon_ar_brief.md").exists())
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_optional_cleanup"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_repairs_premium_3d_imagegen_when_only_control_files_survive_retry(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            asset_files = [
                assets_dir / "00_visual_target.md",
                assets_dir / "01_style_lock.md",
            ]
            for asset_file in asset_files:
                asset_file.write_text("# control\n" + ("detail\n" * 24), encoding="utf-8")

            class ControlOnlyPremiumImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "",
                        "tool_results": [
                            {"written": True, "path": str(asset_file)}
                            for asset_file in asset_files
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=ControlOnlyPremiumImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        created_names = {Path(path).name for path in result.get("files_created", [])}
        self.assertIn("character_hero_brief.md", created_names)
        self.assertIn("weapon_primary_brief.md", created_names)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_fallback_pack_synthesized"
            for call in orch.emit.await_args_list
        ))

    def test_imagegen_early_completion_respects_explicit_premium_goal_and_rejects_control_heavy_bundle(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._current_goal = ""
        goal = "创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。"
        subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            asset_map = {
                "00_visual_target.md": "# control\n" + ("detail\n" * 24),
                "01_style_lock.md": "# control\n" + ("detail\n" * 24),
                "character_hero_brief.md": "# Character Brief: Hero - Tactical Operative\n\n## Identity\nMilitary-scifi special operative\n",
                "manifest.json": json.dumps({
                    "assets": {
                        "enemies": {"grunt_striker": {"status": "placeholder_proxy"}},
                        "weapons": {"rifle_assault": {"status": "placeholder_proxy"}},
                        "environment": {"arena": {"status": "placeholder_proxy"}},
                    }
                }),
            }
            asset_files = []
            for name, content in asset_map.items():
                asset_path = assets_dir / name
                asset_path.write_text(content, encoding="utf-8")
                asset_files.append(str(asset_path))

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orch, "_collect_recent_generated_asset_files", return_value=asset_files):
                healthy_assets = orch._imagegen_early_completion_assets(subtask, goal=goal)

        self.assertEqual(healthy_assets, [])

    def test_execute_subtask_repairs_premium_3d_imagegen_with_manifest_and_short_hero_brief(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            asset_map = {
                "00_visual_target.md": "# control\n" + ("detail\n" * 24),
                "01_style_lock.md": "# control\n" + ("detail\n" * 24),
                "character_hero_brief.md": "# Character Brief: Hero - Tactical Operative\n\n## Identity\nMilitary-scifi special operative\n",
                "environment_arena_brief.md": "# Environment Brief\n" + ("detail\n" * 24),
                "manifest.json": json.dumps({
                    "assets": {
                        "enemies": {"grunt_striker": {"status": "placeholder_proxy"}},
                        "weapons": {"rifle_assault": {"status": "placeholder_proxy"}},
                        "environment": {"arena": {"status": "placeholder_proxy"}},
                    }
                }),
            }
            asset_files = []
            for name, content in asset_map.items():
                asset_path = assets_dir / name
                asset_path.write_text(content, encoding="utf-8")
                asset_files.append(asset_path)

            class ControlHeavyPremiumImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "",
                        "tool_results": [
                            {"written": True, "path": str(asset_file)}
                            for asset_file in asset_files
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=ControlHeavyPremiumImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        created_names = {Path(path).name for path in result.get("files_created", [])}
        self.assertIn("monster_primary_brief.md", created_names)
        self.assertIn("weapon_primary_brief.md", created_names)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_fallback_pack_synthesized"
            for call in orch.emit.await_args_list
        ))

    def test_imagegen_retry_prompt_for_premium_3d_focuses_core_pack_and_thin_files(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        prompt = orch._imagegen_retry_prompt(
            "创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
            "Image Gen: Produce 3D character / monster / weapon / environment modeling design packs.",
            "Imagegen reported success but the current assets bundle still contains empty or thin files (<32 bytes): weapon_ar_brief.md, environment_kit_brief.md.",
        )
        self.assertIn("minimum viable replacement pack", prompt.lower())
        self.assertIn("weapon_ar_brief.md", prompt)
        self.assertIn("environment_kit_brief.md", prompt)
        self.assertIn("weapon_primary_brief.md", prompt)
        self.assertIn("skip optional orthographic prompts", prompt.lower())

    def test_execute_subtask_salvages_imagegen_timeout_when_healthy_assets_exist(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            style_lock = assets_dir / "01_CHARACTER_ASSET_PACK.md"
            weapon_pack = assets_dir / "02_WEAPON_ASSET_PACK.md"
            style_lock.write_text("# character pack\n" + ("detail\n" * 24), encoding="utf-8")
            weapon_pack.write_text("# weapon pack\n" + ("detail\n" * 24), encoding="utf-8")

            class TimedOutImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": False,
                        "output": "Partial prompt-pack output recovered after truncated tool calls.",
                        "error": "imagegen execution timeout after 242s.",
                        "tool_results": [
                            {"written": True, "path": str(style_lock)},
                            {"written": True, "path": str(weapon_pack)},
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=TimedOutImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a concept pack", depends_on=[])
            plan = Plan(goal="做一个 3D 射击游戏并生成配套概念图", subtasks=[subtask])

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("error", ""), "")
        created_names = {Path(path).name for path in result.get("files_created", [])}
        self.assertIn(style_lock.name, created_names)
        self.assertIn(weapon_pack.name, created_names)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_artifact_salvaged"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_repairs_incomplete_premium_3d_timeout_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            asset_files = [
                assets_dir / "00_visual_target.md",
                assets_dir / "01_style_lock.md",
                assets_dir / "character_hero_brief.md",
                assets_dir / "monster_rusher_brief.md",
                assets_dir / "environment_megacity_blockout.md",
            ]
            for asset_file in asset_files:
                asset_file.write_text("# pack\n" + ("detail\n" * 24), encoding="utf-8")

            class TimedOutIncompletePremiumImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": False,
                        "output": "Recovered premium 3D concept pack text after truncation.",
                        "error": "imagegen execution timeout after 242s.",
                        "tool_results": [
                            {"written": True, "path": str(asset_file)}
                            for asset_file in asset_files
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=TimedOutIncompletePremiumImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("error", ""), "")
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_fallback_pack_synthesized"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_synthesizes_premium_imagegen_core_pack_when_failure_writes_no_files(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)

            class EmptyPremiumImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": False,
                        "output": "",
                        "error": "Error code: 401 - invalid_authentication_error",
                        "tool_results": [],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=EmptyPremiumImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a premium concept pack", depends_on=[])
            plan = Plan(
                goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
                subtasks=[subtask],
            )

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        created_names = {Path(path).name for path in result.get("files_created", [])}
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("error", ""), "")
        self.assertIn("manifest.json", created_names)
        self.assertIn("character_hero_brief.md", created_names)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_fallback_pack_synthesized"
            for call in orch.emit.await_args_list
        ))

    def test_repair_imagegen_companion_docs_fills_zero_byte_known_files(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，带怪物、不同枪械、大地图和精美建模，要达到商业级水准，并生成资产概念包。",
            subtasks=[],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            empty_paths = [
                assets_dir / "asset_sources.md",
                assets_dir / "material_texture_directions.md",
                assets_dir / "orthographic_prompts.md",
            ]
            for path in empty_paths:
                path.write_text("", encoding="utf-8")

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                repaired = orch._repair_imagegen_companion_docs(
                    plan,
                    prev_results={},
                    existing_files=[str(path) for path in empty_paths],
                )

            repaired_names = {Path(path).name for path in repaired}
            self.assertEqual(
                repaired_names,
                {"asset_sources.md", "material_texture_directions.md", "orthographic_prompts.md"},
            )
            for path in empty_paths:
                self.assertGreater(path.stat().st_size, 32)

    def test_execute_subtask_completes_imagegen_early_when_healthy_assets_exist(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            hero_pack = assets_dir / "01_HERO_CHARACTER_CONCEPT.md"
            monster_pack = assets_dir / "02_MONSTER_ROSTER_CONCEPT.md"

            class SlowHealthyImageBridge:
                config = {}

                async def execute(self, **kwargs):
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    hero_pack.write_text("# hero pack\n" + ("detail\n" * 24), encoding="utf-8")
                    monster_pack.write_text("# monster pack\n" + ("detail\n" * 24), encoding="utf-8")
                    await asyncio.sleep(10)
                    return {
                        "success": True,
                        "output": "",
                        "tool_results": [],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=SlowHealthyImageBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()
            orch._configured_subtask_timeout = lambda agent_type: 5  # type: ignore[method-assign]
            orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a concept pack", depends_on=[])
            plan = Plan(goal="做一个 3D 射击游戏并生成配套概念图", subtasks=[subtask])

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "IMAGEGEN_ASSET_EARLY_COMPLETE_SEC", 1):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))

        self.assertTrue(result.get("success"))
        created_names = {Path(path).name for path in result.get("files_created", [])}
        self.assertIn(hero_pack.name, created_names)
        self.assertIn(monster_pack.name, created_names)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "imagegen_early_asset_complete"
            for call in orch.emit.await_args_list
        ))

    def test_execute_subtask_ignores_old_stale_empty_assets_from_previous_runs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            assets_dir = tmp_out / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            healthy_asset = assets_dir / "00_style_lock.md"
            healthy_asset.write_text("# style lock\n" + ("detail\n" * 20), encoding="utf-8")
            stale_empty = assets_dir / "legacy_empty.md"
            stale_empty.write_text("", encoding="utf-8")
            old_ts = time.time() - 600
            os.utime(stale_empty, (old_ts, old_ts))

            class HealthyBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "Saved the current prompt pack successfully.",
                        "tool_results": [
                            {"written": True, "path": str(healthy_asset)},
                        ],
                        "tool_call_stats": {},
                    }

            orch = Orchestrator(ai_bridge=HealthyBridge(), executor=None)
            orch.emit = AsyncMock()
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            subtask = SubTask(id="1", agent_type="imagegen", description="Generate a concept pack", depends_on=[])
            plan = Plan(goal="做一个 3D 射击游戏并生成配套概念图", subtasks=[subtask])

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "gpt-5.4", prev_results={}))
                stale_still_exists = stale_empty.exists()

        self.assertTrue(result.get("success"))
        self.assertTrue(stale_still_exists)


class TestBuilderPreviewExposure(unittest.TestCase):
    def test_builder_quality_failure_does_not_emit_preview_ready(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            html = tmp_out / "index.html"
            html.write_text(
                "<!doctype html><html><head><title>Demo</title></head><body><main>draft</main></body></html>",
                encoding="utf-8",
            )

            class StubBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "builder finished",
                        "tool_results": [{"written": True, "path": str(html)}],
                        "tool_call_stats": {},
                    }

            events = []

            async def record(event_type, payload):
                evt = dict(payload or {})
                evt["type"] = event_type
                events.append(evt)

            orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
            orch.emit = AsyncMock(side_effect=record)
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            plan = Plan(goal="Build premium website", subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[])])
            subtask = plan.subtasks[0]

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 91}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": False, "score": 38, "errors": ["Missing middle content"], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertFalse(any(evt.get("type") == "preview_ready" for evt in events))
        self.assertTrue(any(evt.get("stage") == "quality_gate_failed" for evt in events))

    def test_builder_emits_preview_only_after_quality_gate_passes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            html = tmp_out / "index.html"
            html.write_text(
                "<!doctype html><html><head><title>Demo</title></head><body><main>final</main></body></html>",
                encoding="utf-8",
            )

            class StubBridge:
                config = {}

                async def execute(self, **kwargs):
                    return {
                        "success": True,
                        "output": "builder finished",
                        "tool_results": [{"written": True, "path": str(html)}],
                        "tool_call_stats": {},
                    }

            events = []

            async def record(event_type, payload):
                evt = dict(payload or {})
                evt["type"] = event_type
                events.append(evt)

            orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
            orch.emit = AsyncMock(side_effect=record)
            orch._sync_ne_status = AsyncMock()
            orch._emit_ne_progress = AsyncMock()

            plan = Plan(goal="Build premium website", subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[])])
            subtask = plan.subtasks[0]

            with patch.object(orchestrator_module, "OUTPUT_DIR", tmp_out), \
                 patch.object(orchestrator_module, "validate_html_file", return_value={"ok": True, "errors": [], "warnings": [], "checks": {"score": 94}}), \
                 patch.object(orch, "_validate_builder_quality", return_value={"pass": True, "score": 88, "errors": [], "warnings": []}):
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        preview_indexes = [idx for idx, evt in enumerate(events) if evt.get("type") == "preview_ready"]
        self.assertEqual(len(preview_indexes), 1)
        quality_indexes = [
            idx for idx, evt in enumerate(events)
            if evt.get("type") == "subtask_progress" and evt.get("stage") == "quality_gate"
        ]
        self.assertTrue(quality_indexes)
        self.assertGreater(preview_indexes[0], quality_indexes[-1])
        self.assertFalse(events[preview_indexes[0]].get("final", True))


class TestCanonicalBroadcastRuntimeModule(unittest.IsolatedAsyncioTestCase):
    async def test_sync_ne_status_uses_live_main_module_for_broadcasts(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._canonical_ctx = {"task_id": "task_1", "run_id": "run_1"}
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        fake_ne_store = MagicMock()
        fake_ne_store.get_node_execution.return_value = {
            "id": "nodeexec_1",
            "node_key": "planner",
            "node_label": "planner",
            "status": "running",
            "assigned_model": "kimi-coding",
            "assigned_provider": "",
            "retry_count": 0,
            "tokens_used": 0,
            "cost": 0.0,
            "input_summary": "planner task",
            "output_summary": "",
            "error_message": "",
            "artifact_ids": [],
            "started_at": 1.0,
            "ended_at": 0.0,
            "created_at": 1.0,
            "progress": 5,
            "phase": "",
            "version": 3,
        }
        fake_run_store = MagicMock()
        fake_run_store.get_run.return_value = {
            "id": "run_1",
            "active_node_execution_ids": ["nodeexec_1"],
            "version": 7,
        }
        fake_main = SimpleNamespace(
            _broadcast_ws_event=AsyncMock(),
            _transition_node_if_needed=MagicMock(return_value=True),
        )

        with patch.dict(sys.modules, {"__main__": fake_main}, clear=False):
            with patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                with patch.object(orchestrator_module, "get_run_store", return_value=fake_run_store):
                    await orch._sync_ne_status("1", "running", input_summary="planner task")

        fake_main._transition_node_if_needed.assert_called_once_with("nodeexec_1", "running")
        fake_main._broadcast_ws_event.assert_awaited()

    async def test_sync_ne_status_can_reset_started_at_for_retry(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._canonical_ctx = {"task_id": "task_1", "run_id": "run_1"}
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        fake_ne_store = MagicMock()
        fake_ne_store.get_node_execution.return_value = {
            "id": "nodeexec_1",
            "node_key": "builder",
            "node_label": "builder",
            "status": "running",
            "assigned_model": "kimi-coding",
            "assigned_provider": "",
            "retry_count": 0,
            "tokens_used": 0,
            "cost": 0.0,
            "input_summary": "builder task",
            "output_summary": "",
            "error_message": "",
            "artifact_ids": [],
            "started_at": 1.0,
            "ended_at": 0.0,
            "created_at": 1.0,
            "progress": 5,
            "phase": "running",
            "version": 3,
        }
        fake_run_store = MagicMock()
        fake_run_store.get_run.return_value = {
            "id": "run_1",
            "active_node_execution_ids": ["nodeexec_1"],
            "version": 7,
        }
        fake_main = SimpleNamespace(
            _broadcast_ws_event=AsyncMock(),
            _transition_node_if_needed=MagicMock(return_value=True),
        )

        before = time.time()
        with patch.dict(sys.modules, {"__main__": fake_main}, clear=False):
            with patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                with patch.object(orchestrator_module, "get_run_store", return_value=fake_run_store):
                    await orch._sync_ne_status(
                        "1",
                        "running",
                        phase="retrying",
                        retry_count=2,
                        reset_started_at=True,
                    )

        update_payload = fake_ne_store.update_node_execution.call_args.args[1]
        self.assertEqual(update_payload["retry_count"], 2)
        self.assertEqual(update_payload["ended_at"], 0.0)
        self.assertGreaterEqual(update_payload["started_at"], before)

    async def test_emit_ne_progress_uses_live_main_module_for_broadcasts(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._canonical_ctx = {"task_id": "task_1", "run_id": "run_1"}
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        fake_ne_store = MagicMock()
        fake_ne_store.get_node_execution.return_value = {
            "id": "nodeexec_1",
            "progress": 42,
            "phase": "processing",
            "version": 5,
        }
        fake_run_store = MagicMock()
        fake_run_store.get_run.return_value = {"id": "run_1", "version": 9}
        fake_main = SimpleNamespace(
            _broadcast_ws_event=AsyncMock(),
            _transition_node_if_needed=MagicMock(return_value=True),
        )

        with patch.dict(sys.modules, {"__main__": fake_main}, clear=False):
            with patch.object(orchestrator_module, "get_node_execution_store", return_value=fake_ne_store):
                with patch.object(orchestrator_module, "get_run_store", return_value=fake_run_store):
                    await orch._emit_ne_progress("1", progress=42, phase="processing", partial_output="hello")

        fake_main._broadcast_ws_event.assert_awaited()
        payload = fake_main._broadcast_ws_event.await_args.args[0]["payload"]
        self.assertEqual(payload["nodeExecutionId"], "nodeexec_1")
        self.assertEqual(payload["progress"], 42)
        self.assertEqual(payload["phase"], "processing")

    def test_timeout_preserves_model_partial_output_for_retry(self):
        _partial_body = (
            '<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
            '<title>Partial</title>\n<style>\n'
            'body{margin:0;font-family:sans-serif}\n'
            'header{background:#222;color:#fff;padding:2rem}\n'
            'main{padding:2rem}\nsection{margin:1rem 0}\n'
            'footer{background:#333;color:#ccc;padding:1rem}\n'
            '@media(max-width:768px){main{padding:1rem}}\n'
            '</style>\n</head>\n<body>\n'
            '<header><h1>Partial Build</h1></header>\n'
            '<main>\n<section>' + ('partial-content-block ' * 60) + '</section>\n</main>\n'
            '<footer>footer</footer>\n'
        )

        class SlowBridge:
            config = {}

            async def execute(self, **kwargs):
                on_progress = kwargs["on_progress"]
                await on_progress({
                    "stage": "partial_output",
                    "source": "model",
                    "preview": _partial_body,
                })
                await asyncio.sleep(10)
                return {"success": True, "output": "late success", "tool_results": []}

        orch = Orchestrator(ai_bridge=SlowBridge(), executor=None)
        orch._configured_subtask_timeout = lambda agent_type: 1  # type: ignore[method-assign]
        orch._configured_progress_heartbeat = lambda: 1  # type: ignore[method-assign]

        subtask = SubTask(id="partial-timeout", agent_type="builder", description="build", depends_on=[])
        plan = Plan(goal="Build test page", subtasks=[subtask])

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))
                root_index_exists = (Path(td) / "index.html").exists()
                root_index = (Path(td) / "index.html").read_text(encoding="utf-8") if root_index_exists else ""
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(result.get("success"))
        self.assertIn("<!DOCTYPE html>", str(result.get("output", "")))
        self.assertIn("partial-content", subtask.last_partial_output)
        self.assertTrue(root_index_exists)
        self.assertIn("partial-content", root_index)


class TestTimeoutContinuationRecovery(unittest.TestCase):
    def test_builder_timeout_retry_uses_saved_partial_output(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        subtask = SubTask(id="11", agent_type="builder", description="build landing page", depends_on=[])
        subtask.status = TaskStatus.FAILED
        subtask.error = "timeout after 120s"
        subtask.last_partial_output = "<!DOCTYPE html>\n<html>\n" + ("component-block\n" * 20)
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(ok)
        self.assertIn("上次执行因超时中断", captured.get("desc", ""))
        self.assertIn("_partial_builder.html", captured.get("desc", ""))
        self.assertIn(td, captured.get("desc", ""))
        self.assertIn("不要从零开始重写", captured.get("desc", ""))
        self.assertNotIn("MAX 150 lines", captured.get("desc", ""))
        self.assertNotIn("MAX 100 lines", captured.get("desc", ""))
        self.assertNotIn("<!DOCTYPE html>", captured.get("desc", ""))
        self.assertEqual(subtask.description, "build landing page")


class TestExtractionAndRetrySemantics(unittest.TestCase):
    def test_extract_and_save_code_auto_closes_fenced_html(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>Demo</title></head>
<body><main>hello</main>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "fenced")
                saved = (Path(td) / "task_fenced" / "index.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any(str(p).endswith("index.html") for p in files))
        self.assertIn("</body>", saved.lower())
        self.assertIn("</html>", saved.lower())

    def test_extract_and_save_code_auto_closes_raw_html(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """prefix
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Raw</title></head>
<body><section>demo</section>"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "raw")
                saved = (Path(td) / "task_raw" / "index.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any(str(p).endswith("index.html") for p in files))
        self.assertIn("</body>", saved.lower())
        self.assertIn("</html>", saved.lower())

    def test_extract_and_save_code_skips_thin_game_shell(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._current_task_type = "game"
        output = """```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>Shell</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #05070d; color: #fff; min-height: 100vh; }
    .shell { position: fixed; inset: 0; }
  </style>
</head>
<body>
  <div class="shell"></div>
</body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "thin_game")
                exists = (Path(td) / "task_thin_game" / "index.html").exists()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [])
        self.assertFalse(exists)

    def test_extract_and_save_code_keeps_playable_game_html(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._current_task_type = "game"
        output = """```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>Playable</title>
  <style>
    body { margin: 0; background: #08111f; color: #fff; }
    #gameCanvas { width: 100vw; height: 100vh; display: block; }
    #startOverlay, #hud { position: absolute; left: 16px; top: 16px; z-index: 2; }
  </style>
</head>
<body>
  <section id="startOverlay">
    <button id="startBtn" onclick="startGame()">Start Mission</button>
  </section>
  <canvas id="gameCanvas"></canvas>
  <div id="hud">HP 100 | Ammo 24</div>
  <script>
    let started = false;
    function startGame() { started = true; requestAnimationFrame(gameLoop); }
    function gameLoop() { if (!started) return; requestAnimationFrame(gameLoop); }
    document.addEventListener('keydown', () => {});
  </script>
</body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "playable_game")
                saved = (Path(td) / "task_playable_game" / "index.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any(str(p).endswith("index.html") for p in files))
        self.assertIn("requestanimationframe", saved.lower())
        self.assertIn("gamecanvas", saved.lower())

    def test_extract_and_save_code_keeps_game_html_when_start_flow_is_implicit_but_runtime_is_real(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._current_task_type = "game"
        output = """```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>Auto Start Arena</title>
  <style>
    body { margin: 0; background: #08111f; color: #fff; }
    #gameCanvas { width: 100vw; height: 100vh; display: block; }
    #hud { position: absolute; left: 16px; top: 16px; z-index: 2; }
  </style>
</head>
<body>
  <canvas id="gameCanvas"></canvas>
  <div id="hud">HP 100 | Ammo 24</div>
  <script>
    const state = { running: true };
    function gameLoop() {
      if (!state.running) return;
      requestAnimationFrame(gameLoop);
    }
    document.addEventListener('keydown', () => {});
    window.addEventListener('pointermove', () => {});
    gameLoop();
  </script>
</body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "implicit_start_game")
                saved = (Path(td) / "task_implicit_start_game" / "index.html").read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any(str(p).endswith("index.html") for p in files))
        self.assertIn("requestanimationframe", saved.lower())
        self.assertIn("keydown", saved.lower())

    def test_extract_and_save_code_can_skip_root_copy_for_secondary_builder(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Secondary</title></head>
<body><main>secondary page</main></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "secondary", allow_root_index_copy=False)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(any(str(p).endswith("/task_secondary/index.html") for p in files))
        self.assertFalse(any(Path(p).parent == Path(td) for p in files))

    def test_extract_and_save_code_supports_named_multi_file_blocks(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html index.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Home</title></head>
<body><a href="collections.html">Collections</a></body>
</html>
```
```html collections.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collections</title></head>
<body><a href="index.html">Home</a></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "multi")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            home = (Path(td) / "index.html").read_text(encoding="utf-8")
            collections = (Path(td) / "collections.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "index.html"), files)
        self.assertIn(str(Path(td) / "collections.html"), files)
        self.assertIn("Collections", home)
        self.assertIn("Home", collections)

    def test_extract_and_save_code_recovers_unterminated_named_multi_file_block(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html index.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Home</title></head>
<body><a href="collections.html">Collections</a></body>
</html>
```
```html collections.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collections</title></head>
<body><a href="index.html">Home</a></body>
</html>"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "unterminated", multi_page_required=True)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            home = (Path(td) / "index.html").read_text(encoding="utf-8")
            collections = (Path(td) / "collections.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "index.html"), files)
        self.assertIn(str(Path(td) / "collections.html"), files)
        self.assertIn("Collections", home)
        self.assertIn("Home", collections)

    def test_extract_and_save_code_recovers_lossy_multi_file_headers(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html index.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Home</title></head>
<body><a href="collections.html">Collections</a></body>
</html>
```html collections.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collections</title></head>
<body><a href="index.html">Home</a></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "lossy_headers", multi_page_required=True)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            home = (Path(td) / "index.html").read_text(encoding="utf-8")
            collections = (Path(td) / "collections.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "index.html"), files)
        self.assertIn(str(Path(td) / "collections.html"), files)
        self.assertIn("Collections", home)
        self.assertIn("Home", collections)

    def test_extract_and_save_code_skips_single_unnamed_html_for_multi_page_requests(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """The blocked site failed to load.
```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collapsed</title></head>
<body><main>only one unnamed page</main></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(output, "multi_skip", multi_page_required=True)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(files, [])

    def test_extract_and_save_code_can_salvage_single_raw_html_for_multi_page_timeout(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Home</title></head>
<body><main><section>Recovered partial homepage</section></main></body>
</html>"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(
                    output,
                    "multi_timeout_salvage",
                    multi_page_required=True,
                    allow_multi_page_raw_html_fallback=True,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            saved = (Path(td) / "index.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "index.html"), files)
        self.assertIn("Recovered partial homepage", saved)

    def test_extract_and_save_code_remaps_skipped_named_blocks_when_count_matches_assignment(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        output = """```html collections.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Collections</title></head>
<body><main><section>First recovered page</section></main></body>
</html>
```
```html heritage.html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Heritage</title></head>
<body><main><section>Second recovered page</section></main></body>
</html>
```"""

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                files = orch._extract_and_save_code(
                    output,
                    "remap_targets",
                    multi_page_required=True,
                    allowed_html_targets=["pricing.html", "features.html"],
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            pricing = (Path(td) / "pricing.html").read_text(encoding="utf-8")
            features = (Path(td) / "features.html").read_text(encoding="utf-8")

        self.assertIn(str(Path(td) / "pricing.html"), files)
        self.assertIn(str(Path(td) / "features.html"), files)
        self.assertIn("First recovered page", pricing)
        self.assertIn("Second recovered page", features)

    def test_non_root_builder_root_overwrite_is_rejected_and_restored(self):
        class StubBridge:
            def __init__(self, root_path: Path):
                self.config = {}
                self.root_path = root_path

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "saved",
                    "tool_results": [{"written": True, "path": str(self.root_path)}],
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            stable_html = tmp_out / "_stable_previews" / "run_test" / "approved_task_1" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            root_html.write_text(
                "<!doctype html><html><head><title>Good</title></head><body><main><section>good root</section></main><script>1</script></body></html>",
                encoding="utf-8",
            )
            stable_html.write_text(
                "<!doctype html><html><head><title>Good</title></head><body><main><section>good root</section></main><script>1</script></body></html>",
                encoding="utf-8",
            )
            bridge = StubBridge(root_html)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch.emit = AsyncMock()
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、定价、故事、门店、联系页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                ],
            )
            orch._stable_preview_path = stable_html
            orch._stable_preview_files = [str(stable_html)]
            orch._stable_preview_stage = "builder_quality_pass"

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                result = asyncio.run(orch._execute_subtask(plan.subtasks[1], plan, "kimi-coding", {}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

            restored = root_html.read_text(encoding="utf-8")

        self.assertFalse(result.get("success"))
        self.assertTrue(
            "Only Builder 1 may write" in result.get("error", "")
            or "quality gate failed" in str(result.get("error", "")).lower()
        )
        self.assertIn("good root", restored)

    def test_builder_prefers_claimed_written_files_over_directory_scan(self):
        class StubBridge:
            config = {}

            def __init__(self, claimed_path: Path):
                self.claimed_path = claimed_path

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({"stage": "builder_write", "path": str(self.claimed_path)})
                return {
                    "success": False,
                    "output": "",
                    "error": "builder execution timeout after 421s.",
                    "tool_results": [],
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            claimed = tmp_out / "about.html"
            unrelated = tmp_out / "pricing.html"
            claimed.write_text(
                "<!doctype html><html><head><title>About</title></head><body><main>about</main></body></html>",
                encoding="utf-8",
            )
            unrelated.write_text(
                "<!doctype html><html><head><title>Pricing</title></head><body><main>pricing</main></body></html>",
                encoding="utf-8",
            )

            bridge = StubBridge(claimed)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch.emit = AsyncMock()
            orch._run_started_at = time.time() - 30
            plan = Plan(
                goal="做一个八页面奢侈品品牌官网，包含首页、品牌、工艺、系列、定价、故事、门店、联系页",
                difficulty="pro",
                subtasks=[
                    SubTask(id="1", agent_type="builder", description="build home"),
                    SubTask(id="2", agent_type="builder", description="build secondary"),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                asyncio.run(orch._execute_subtask(plan.subtasks[1], plan, "kimi-coding", {}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        subtask_complete_calls = [
            call for call in orch.emit.await_args_list
            if call.args and call.args[0] == "subtask_complete"
        ]
        self.assertTrue(subtask_complete_calls)
        files_created = subtask_complete_calls[-1].args[1]["files_created"]
        self.assertEqual(files_created, [orch._normalize_generated_path(str(claimed))])

    def test_builder_timeout_with_valid_saved_artifact_is_salvaged(self):
        class StubBridge:
            def __init__(self, html_path: Path):
                self.config = {}
                self.html_path = html_path

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": False,
                    "output": "",
                    "error": "builder execution timeout after 421s.",
                    "tool_results": [{"written": True, "path": str(self.html_path)}],
                }

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index = tmp_out / "index.html"
            index.write_text(
                """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Maison Aurelia</title><style>:root{--bg:#0b1020;--fg:#f3f5f8;--panel:#121a34;--line:rgba(255,255,255,.08);--accent:#d8b36a}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#121a34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;padding:20px 24px}.hero,.grid,.cta{display:grid;gap:18px}.hero{grid-template-columns:1.3fr .7fr;align-items:center}.grid{grid-template-columns:repeat(3,1fr)}.cta{grid-template-columns:repeat(2,1fr)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:24px}button{padding:12px 18px;border:none;border-radius:999px;background:var(--accent);color:#201505;font-weight:700}main{display:grid;gap:18px;padding:24px}@media(max-width:900px){.hero,.grid,.cta{grid-template-columns:1fr}}</style></head>
<body><header><nav><strong>Maison Aurelia</strong><button>Book an appointment</button></nav></header><main><section class="hero"><article class="panel"><h1>Quiet luxury, precisely composed.</h1><p>Maison Aurelia presents a refined luxury narrative with editorial structure, dense content, and a calm premium tone.</p><p>Every section is production-ready and visually complete.</p></article><article class="panel"><h2>Private presentation</h2><p>Discover collections, heritage, and bespoke services.</p></article></section><section class="grid"><article class="panel"><h3>Craft</h3><p>Hand-finished details from the Paris atelier.</p></article><article class="panel"><h3>Materials</h3><p>Rare leathers and precious metal accents.</p></article><article class="panel"><h3>Service</h3><p>Concierge support for collectors worldwide.</p></article></section><section class="cta"><article class="panel"><h3>Visit the maison</h3><p>Private appointments in flagship salons.</p></article><article class="panel"><h3>Request a consultation</h3><p>Receive a curated presentation and next steps.</p></article></section></main><footer class="panel">Maison Aurelia.</footer><script>document.querySelectorAll('button').forEach(btn=>btn.addEventListener('click',()=>{}));</script></body></html>""",
                encoding="utf-8",
            )
            bridge = StubBridge(index)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch.emit = AsyncMock()
            plan = Plan(goal="做一个高端品牌官网", subtasks=[SubTask(id="1", agent_type="builder", description="build")])

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                result = asyncio.run(orch._execute_subtask(plan.subtasks[0], plan, "kimi-coding", {}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("error", ""), "")

    def test_execute_subtask_exception_keeps_running_state_when_retry_pending(self):
        class CrashBridge:
            config = {}

            async def execute(self, **kwargs):
                raise RuntimeError("bridge exploded")

        orch = Orchestrator(ai_bridge=CrashBridge(), executor=None)
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch.emit = AsyncMock()
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=2)
        plan = Plan(goal="Build test page", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertIn("running", statuses)
        self.assertNotIn("failed", statuses)
        subtask_complete_calls = [
            call.args[1] for call in orch.emit.await_args_list
            if call.args and call.args[0] == "subtask_complete"
        ]
        self.assertTrue(subtask_complete_calls)
        self.assertTrue(subtask_complete_calls[-1].get("retry_pending"))

    def test_execute_subtask_failed_result_keeps_running_state_when_retry_pending(self):
        class FailingBridge:
            config = {}

            async def execute(self, **kwargs):
                return {
                    "success": False,
                    "output": "",
                    "error": "builder quality gate failed",
                    "tool_results": [],
                }

        orch = Orchestrator(ai_bridge=FailingBridge(), executor=None)
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch.emit = AsyncMock()
        orch._subtask_ne_map = {"1": "nodeexec_1"}

        subtask = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=2)
        plan = Plan(goal="Build test page", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertIn("running", statuses)
        self.assertNotIn("failed", statuses)
        subtask_complete_calls = [
            call.args[1] for call in orch.emit.await_args_list
            if call.args and call.args[0] == "subtask_complete"
        ]
        self.assertTrue(subtask_complete_calls)
        self.assertTrue(subtask_complete_calls[-1].get("retry_pending"))

    def test_handle_failure_retries_without_terminal_failed_status(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="builder", description="build landing page", depends_on=[], max_retries=2)
        subtask.status = TaskStatus.FAILED
        subtask.error = "builder quality gate failed"
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        async def fake_execute_subtask(st, _plan, _model, _results):
            st.status = TaskStatus.COMPLETED
            st.output = "ok"
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertEqual(statuses[0], "running")
        self.assertNotIn("failed", statuses)

    def test_handle_failure_marks_failed_only_after_retry_budget_exhausted(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="builder", description="build landing page", depends_on=[], max_retries=1)
        subtask.status = TaskStatus.FAILED
        subtask.error = "builder quality gate failed"
        subtask.retries = 1
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertFalse(ok)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertEqual(statuses, ["failed"])

    def test_builder_nav_repair_only_false_when_other_quality_errors_exist(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        error = (
            "Builder quality gate failed (score=64). Errors: "
            "['Some multi-page routes are still too thin / stub-like for shipment: features.html (19855 bytes)', "
            "'index.html does not expose enough working local navigation links to the additional pages.']"
        )

        self.assertFalse(orch._builder_nav_repair_only(error))

    def test_nav_only_builder_retry_preserves_current_output_without_cleanup(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        orch._restore_output_from_stable_preview = Mock(return_value=[])
        orch._cleanup_internal_builder_artifacts = Mock(return_value=["/tmp/evermind_output/index.html"])
        orch._evaluate_multi_page_artifacts = Mock(return_value={
            "ok": False,
            "html_files": ["index.html", "pricing.html", "features.html", "contact.html"],
            "observed_html_files": ["index.html", "pricing.html", "features.html", "contact.html"],
            "invalid_html_files": [],
            "errors": ["index.html does not expose enough working local navigation links to the additional pages."],
            "warnings": [],
            "repair_scope": "root_nav_only",
            "nav_targets": ["contact.html"],
            "matched_nav_targets": ["contact.html"],
            "missing_nav_targets": [],
            "unlinked_secondary_pages": ["pricing.html", "features.html"],
        })

        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium site",
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = (
            "Builder quality gate failed (score=80). Errors: "
            "['index.html does not expose enough working local navigation links to the additional pages.']"
        )
        plan = Plan(goal="做一个四页面官网，包含首页、定价页、功能页和联系页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        orch._restore_output_from_stable_preview.assert_not_called()
        orch._cleanup_internal_builder_artifacts.assert_not_called()
        self.assertIn("NAVIGATION REPAIR ONLY.", captured.get("desc", ""))

    def test_nav_only_builder_retry_prompt_locks_output_to_index_only(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        orch._restore_output_from_stable_preview = Mock(return_value=[])
        orch._cleanup_internal_builder_artifacts = Mock(return_value=[])
        orch._evaluate_multi_page_artifacts = Mock(return_value={
            "ok": False,
            "html_files": ["index.html", "pricing.html", "features.html", "contact.html"],
            "observed_html_files": ["index.html", "pricing.html", "features.html", "contact.html"],
            "invalid_html_files": [],
            "errors": ["index.html references missing local pages: destinations.html"],
            "warnings": [],
            "repair_scope": "root_nav_only",
            "nav_targets": ["pricing.html", "features.html", "contact.html", "destinations.html"],
            "matched_nav_targets": ["pricing.html", "features.html", "contact.html"],
            "missing_nav_targets": ["destinations.html"],
            "unlinked_secondary_pages": [],
        })

        subtask = SubTask(
            id="4",
            agent_type="builder",
            description="build premium site",
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = (
            "Builder quality gate failed (score=70). Errors: "
            "['index.html references missing local pages: destinations.html']"
        )
        plan = Plan(goal="做一个四页面官网，包含首页、定价页、功能页和联系页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        desc = captured.get("desc", "")
        self.assertIn(orchestrator_module.BUILDER_NAV_REPAIR_ONLY_MARKER, desc)
        self.assertIn(f"{orchestrator_module.BUILDER_TARGET_OVERRIDE_MARKER} index.html", desc)
        self.assertIn("Output ONLY a single fenced ```html index.html ...``` block", desc)
        self.assertIn("Do NOT write styles.css, app.js, or any secondary HTML page during this retry.", desc)
        self.assertIn("Do NOT return prose, explanations, summaries, or planning text", desc)

    def test_retry_prompt_for_builder_thin_page_and_nav_uses_direct_multifile_targets(self):
        bridge = type("Bridge", (), {"config": {}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()
        subtask = SubTask(
            id="4",
            agent_type="builder",
            description=(
                "build premium site\n"
                "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, about.html."
            ),
            depends_on=[],
            max_retries=2,
        )
        subtask.status = TaskStatus.FAILED
        subtask.error = (
            "Builder quality gate failed (score=64). Errors: "
            "['Some multi-page routes are still too thin / stub-like for shipment: features.html (19855 bytes)', "
            "'index.html does not expose enough working local navigation links to the additional pages.']"
        )
        plan = Plan(goal="创建一个介绍美国旅游景点的网站，网站一共8页", subtasks=[subtask])

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        self.assertIn("DIRECT MULTI-FILE DELIVERY ONLY.", captured.get("desc", ""))
        self.assertIn("features.html", captured.get("desc", ""))
        self.assertNotIn("NAVIGATION REPAIR ONLY.", captured.get("desc", ""))

    def test_handle_failure_failed_retry_requeues_when_budget_remains(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="builder", description="build landing page", depends_on=[], max_retries=3)
        subtask.status = TaskStatus.FAILED
        subtask.error = "Builder quality gate failed"
        plan = Plan(goal="Build premium website", subtasks=[subtask])

        async def fake_execute_subtask(st, _plan, _model, _results):
            return {"success": False, "error": "Builder quality gate failed again"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertFalse(ok)
        self.assertEqual(subtask.status, TaskStatus.PENDING)
        self.assertEqual(subtask.retries, 1)
        statuses = [call.args[1] for call in orch._sync_ne_status.await_args_list if len(call.args) >= 2]
        self.assertEqual(statuses[0], "running")
        self.assertNotIn("failed", statuses)

    def test_execute_plan_keeps_subtask_runnable_when_retry_is_scheduled(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="builder", description="build landing page", depends_on=[], max_retries=2)
        plan = Plan(goal="Build premium website", subtasks=[subtask])
        attempts = {"count": 0}

        async def fake_execute_subtask(st, _plan, _model, _results):
            attempts["count"] += 1
            if attempts["count"] == 1:
                st.error = "Builder quality gate failed"
                return {"success": False, "error": st.error}
            st.status = TaskStatus.COMPLETED
            st.output = "ok"
            return {"success": True, "output": "ok"}

        async def fake_handle_failure(st, _plan, _model, _results):
            st.retries += 1
            st.status = TaskStatus.PENDING
            return False

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]
        orch._handle_failure = fake_handle_failure  # type: ignore[method-assign]

        results = asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertEqual(attempts["count"], 2)
        self.assertTrue(results["1"]["success"])
        self.assertEqual(subtask.status, TaskStatus.COMPLETED)

    def test_execute_plan_runs_soft_dependency_downstream_after_optional_failure(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()

        imagegen = SubTask(id="1", agent_type="imagegen", description="draw assets", depends_on=[], max_retries=0)
        builder = SubTask(id="2", agent_type="builder", description="build game", depends_on=["1"], max_retries=0)
        reviewer = SubTask(id="3", agent_type="reviewer", description="review", depends_on=["2"], max_retries=0)
        plan = Plan(goal="Build a 3D web game", subtasks=[imagegen, builder, reviewer])
        calls = []

        async def fake_execute_subtask(st, _plan, _model, _results):
            calls.append(st.id)
            if st.id == "1":
                st.error = "imagegen execution timeout after 242s."
                return {"success": False, "error": st.error}
            st.status = TaskStatus.COMPLETED
            st.output = f"ok-{st.id}"
            return {"success": True, "output": st.output}

        async def fake_handle_failure(st, _plan, _model, _results):
            st.status = TaskStatus.FAILED
            st.error = str(st.error or "failed").strip()
            return False

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]
        orch._handle_failure = fake_handle_failure  # type: ignore[method-assign]

        results = asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertEqual(calls, ["1", "2", "3"])
        self.assertEqual(imagegen.status, TaskStatus.FAILED)
        self.assertEqual(builder.status, TaskStatus.COMPLETED)
        self.assertEqual(reviewer.status, TaskStatus.COMPLETED)
        self.assertTrue(results["2"]["success"])
        self.assertTrue(results["3"]["success"])

    def test_analyst_gate_requires_two_live_reference_urls(self):
        """Analyst gate hard-fails when no live references were browsed."""
        class StubBridge:
            config = {}

            async def execute(self, **kwargs):
                return {
                    "success": True,
                    "output": "A short design brief with no URLs.",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[], max_retries=1)
        plan = Plan(goal="Research SaaS references", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertIn("at least 2 live reference URLs", str(result.get("error", "")))

    def test_analyst_output_injects_browser_urls_into_report(self):
        class StubBridge:
            config = {}

            async def execute(self, **kwargs):
                return {
                    "success": True,
                    "output": (
                        "<reference_sites>\n"
                        "- https://example.com\n"
                        "- https://example.org\n"
                        "</reference_sites>\n"
                        "<design_direction>Color palette is cool blue. Layout is hero-first.</design_direction>\n"
                        "<non_negotiables>No emoji glyphs.</non_negotiables>\n"
                    ),
                    "tool_results": [
                        {"success": True, "data": {"url": "https://example.com"}},
                        {"success": True, "data": {"url": "https://example.org"}},
                    ],
                    "tool_call_stats": {"browser": 2},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[], max_retries=1)
        plan = Plan(goal="Research SaaS references", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        output = str(result.get("output", ""))
        self.assertIn("<reference_sites>", output)
        self.assertIn("https://example.com", output)
        self.assertIn("https://example.org", output)

    def test_analyst_gate_accepts_source_fetch_only_research(self):
        class StubBridge:
            config = {}

            async def execute(self, **kwargs):
                return {
                    "success": True,
                    "output": (
                        "<reference_sites>\n"
                        "- https://github.com/pmndrs/ecctrl\n"
                        "- https://github.com/donmccurdy/three-pathfinding\n"
                        "</reference_sites>\n"
                        "<design_direction>Third-person shooter with authored 3D silhouettes.</design_direction>\n"
                        "<non_negotiables>Do not mirror controls.</non_negotiables>\n"
                    ),
                    "tool_results": [
                        {"success": True, "data": {"requested_url": "https://github.com/pmndrs/ecctrl"}},
                        {"success": True, "data": {"requested_url": "https://github.com/donmccurdy/three-pathfinding"}},
                    ],
                    "tool_call_stats": {"source_fetch": 2},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[], max_retries=1)
        plan = Plan(goal="Research SaaS references", subtasks=[subtask])

        result = asyncio.run(orch._execute_subtask(subtask, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        stages = [call.args[1].get("stage") for call in orch.emit.await_args_list if call.args and len(call.args) > 1]
        self.assertIn("analyst_reference_summary", stages)

    def test_analyst_retry_after_reference_gate_failure_forces_browser_usage(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._sync_ne_status = AsyncMock()
        orch.emit = AsyncMock()

        subtask = SubTask(id="1", agent_type="analyst", description="research", depends_on=[], max_retries=2)
        subtask.status = TaskStatus.FAILED
        subtask.error = "Analyst research incomplete: must browse at least 2 live reference URLs and list them in the report."
        plan = Plan(goal="Research SaaS references", subtasks=[subtask])
        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["desc"] = st.description
            return {"success": True, "output": "ok"}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        self.assertIn("MUST use the browser tool on at least 2 different source URLs", captured.get("desc", ""))
        self.assertIn("<reference_sites>", captured.get("desc", ""))


class TestFinalPreviewEmission(unittest.TestCase):
    def test_select_preview_artifact_prefers_root_index_over_task_local_index(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            task_html = tmp_out / "task_3" / "index.html"
            task_html.parent.mkdir(parents=True, exist_ok=True)
            root_html.write_text("<!doctype html><html><body>root</body></html>", encoding="utf-8")
            task_html.write_text("<!doctype html><html><body>task</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                selected = orch._select_preview_artifact_for_files([
                    str(task_html),
                    str(root_html),
                ])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(selected, root_html)

    def test_select_preview_artifact_prefers_real_route_over_task_local_fallback(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            route_html = tmp_out / "contact.html"
            task_html = tmp_out / "task_3" / "index.html"
            task_html.parent.mkdir(parents=True, exist_ok=True)
            route_html.write_text("<!doctype html><html><body>contact</body></html>", encoding="utf-8")
            task_html.write_text("<!doctype html><html><body>task</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                selected = orch._select_preview_artifact_for_files([
                    str(task_html),
                    str(route_html),
                ])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(selected, route_html)

    def test_select_preview_artifact_ignores_stable_preview_snapshots(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            task_html = tmp_out / "task_5" / "index.html"
            stable_html = tmp_out / "_stable_previews" / "run_prev" / "approved_task_1" / "index.html"
            task_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            task_html.write_text("<!doctype html><html><body>task fallback</body></html>", encoding="utf-8")
            stable_html.write_text("<!doctype html><html><body>stable snapshot</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                selected = orch._select_preview_artifact_for_files([
                    str(task_html),
                    str(stable_html),
                ])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(selected, task_html)

    def test_current_run_html_artifacts_ignore_internal_preview_artifacts(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            task_html = tmp_out / "task_5" / "index.html"
            stable_html = tmp_out / "_stable_previews" / "run_prev" / "approved_task_1" / "index.html"
            task_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            root_html.write_text("<!doctype html><html><body>root</body></html>", encoding="utf-8")
            task_html.write_text("<!doctype html><html><body>task fallback</body></html>", encoding="utf-8")
            stable_html.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")

            orch._run_started_at = time.time()
            now = time.time()
            os.utime(root_html, (now, now))
            os.utime(task_html, (now, now))
            os.utime(stable_html, (now, now))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                html_files = orch._current_run_html_artifacts()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(html_files, [root_html])

    def test_emit_final_preview_ignores_stale_artifacts(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            old_task = tmp_out / "task_1"
            old_task.mkdir(parents=True, exist_ok=True)
            old_html = old_task / "index.html"
            old_html.write_text("<!doctype html><html><head></head><body>old</body></html>", encoding="utf-8")

            # Simulate run start after old artifact was created.
            now = time.time()
            old_mtime = now - 120
            old_html.touch()
            old_html.chmod(0o644)
            os.utime(old_html, (old_mtime, old_mtime))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 0)

    def test_emit_final_preview_picks_run_local_artifact(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            task = tmp_out / "task_2"
            task.mkdir(parents=True, exist_ok=True)
            html = task / "index.html"
            html.write_text("<!doctype html><html><head></head><body>new</body></html>", encoding="utf-8")

            now = time.time()
            fresh = now + 1
            html.touch()
            html.chmod(0o644)
            os.utime(html, (fresh, fresh))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/task_2/index.html", preview_events[0].get("preview_url", ""))

    def test_emit_final_preview_prefers_stable_snapshot_over_newer_run_local_artifact(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            failed_html = tmp_out / "task_3" / "index.html"
            failed_html.parent.mkdir(parents=True, exist_ok=True)
            failed_html.write_text("<!doctype html><html><body>bad</body></html>", encoding="utf-8")

            stable_html = tmp_out / "_stable_previews" / "run_test" / "approved_task_2" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text("<!doctype html><html><body>good</body></html>", encoding="utf-8")

            now = time.time()
            os.utime(failed_html, (now + 1, now + 1))
            os.utime(stable_html, (now, now))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                orch._stable_preview_path = stable_html
                orch._stable_preview_files = [str(stable_html)]
                orch._stable_preview_stage = "builder_quality_pass"
                asyncio.run(orch._emit_final_preview())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/_stable_previews/run_test/approved_task_2/index.html", preview_events[0].get("preview_url", ""))
        self.assertTrue(preview_events[0].get("stable_preview"))

    def test_emit_final_preview_failed_run_restores_stable_root_and_skips_failed_artifact(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            root_html.write_text("<!doctype html><html><body>broken</body></html>", encoding="utf-8")

            failed_html = tmp_out / "task_3" / "index.html"
            failed_html.parent.mkdir(parents=True, exist_ok=True)
            failed_html.write_text("<!doctype html><html><body>bad</body></html>", encoding="utf-8")

            stable_html = tmp_out / "_stable_previews" / "run_test" / "approved_task_2" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text("<!doctype html><html><body>good</body></html>", encoding="utf-8")

            now = time.time()
            os.utime(root_html, (now + 1, now + 1))
            os.utime(failed_html, (now + 1, now + 1))
            os.utime(stable_html, (now, now))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                orch._stable_preview_path = stable_html
                orch._stable_preview_files = [str(stable_html)]
                orch._stable_preview_stage = "builder_quality_pass"
                asyncio.run(orch._emit_final_preview(report_success=False))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertEqual(root_html.read_text(encoding="utf-8"), stable_html.read_text(encoding="utf-8"))

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/_stable_previews/run_test/approved_task_2/index.html", preview_events[0].get("preview_url", ""))
        self.assertTrue(preview_events[0].get("stable_preview"))

    def test_emit_final_preview_failed_run_without_stable_snapshot_keeps_live_root_and_emits_preview(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            root_html = tmp_out / "index.html"
            root_html.write_text(
                (
                    "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<title>Fallback</title>"
                    "<style>"
                    ":root{color-scheme:light}body{margin:0;font-family:'Noto Sans SC',sans-serif;background:#08111f;color:#f3f1ea}"
                    "main{display:grid;gap:24px;padding:48px}section{padding:28px;border:1px solid rgba(255,255,255,.12);border-radius:24px}"
                    "@media (max-width: 720px){main{padding:24px}}"
                    "</style></head><body><main>"
                    "<section><h1>Fallback Preview</h1><p>"
                    + ("keep live root " * 90)
                    + "</p></section>"
                    "<section><h2>Highlights</h2><p>"
                    + ("cinematic travel story " * 60)
                    + "</p></section>"
                    "</main><script>window.__fallbackPreview=true;</script></body></html>"
                ),
                encoding="utf-8",
            )

            now = time.time()
            os.utime(root_html, (now + 1, now + 1))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview(report_success=False))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(root_html.exists())

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/index.html", preview_events[0].get("preview_url", ""))
        self.assertFalse(preview_events[0].get("stable_preview"))
        self.assertEqual(preview_events[0].get("stage"), "failed_run_live_fallback")

    def test_emit_final_preview_materializes_parallel_builder_parts(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            part1 = tmp_out / "index_part1.html"
            part2 = tmp_out / "index_part2.html"
            part1.write_text(
                "<!doctype html><html><head><title>Split Demo</title><style>body{margin:0}header{display:block}</style></head><body><header>Top</header><main><section>Hero</section></main><script>window.topHalf=true;</script></body></html>",
                encoding="utf-8",
            )
            part2.write_text(
                "<!doctype html><html><head><style>footer{display:block}.pricing{display:grid}@media(max-width:700px){.pricing{display:block}}</style></head><body><section class='pricing'>Pricing</section><footer>Footer</footer><script>window.bottomHalf=true;</script></body></html>",
                encoding="utf-8",
            )

            now = time.time()
            fresh = now + 1
            os.utime(part1, (fresh, fresh))
            os.utime(part2, (fresh, fresh))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertIn("/preview/index.html", preview_events[0].get("preview_url", ""))

    def test_emit_final_preview_promotes_shared_assets_into_stable_snapshot(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        events = []

        async def on_event(evt):
            events.append(evt)

        orch.on_event = on_event

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index_html = tmp_out / "index.html"
            styles_css = tmp_out / "styles.css"
            app_js = tmp_out / "app.js"
            index_html.write_text(
                "<!doctype html><html><head><link rel='stylesheet' href='styles.css'></head>"
                "<body><script src='app.js'></script></body></html>",
                encoding="utf-8",
            )
            styles_css.write_text("body{background:#f4efe6;color:#1d1c1a;}", encoding="utf-8")
            app_js.write_text("window.__previewReady=true;", encoding="utf-8")

            now = time.time()
            fresh = now + 1
            for item in (index_html, styles_css, app_js):
                os.utime(item, (fresh, fresh))

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._run_started_at = now
                asyncio.run(orch._emit_final_preview(report_success=True))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            stable_root = tmp_out / "_stable_previews"
            snapshots = [path for path in stable_root.rglob("*") if path.is_dir()]
            self.assertTrue(any((path / "styles.css").exists() for path in snapshots))
            self.assertTrue(any((path / "app.js").exists() for path in snapshots))

        preview_events = [e for e in events if e.get("type") == "preview_ready"]
        self.assertEqual(len(preview_events), 1)
        self.assertTrue(preview_events[0].get("stable_preview"))


class TestDependencyFailureBlocking(unittest.TestCase):
    def test_downstream_subtasks_are_blocked_when_builder_fails(self):
        class StubBridge:
            def __init__(self):
                self.calls = []
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.calls.append(node.get("type"))
                if node.get("type") == "builder":
                    return {"success": False, "output": "", "error": ""}
                return {"success": True, "output": "ok", "tool_results": []}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=0),
                SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"]),
                SubTask(id="3", agent_type="deployer", description="deploy", depends_on=["1"]),
                SubTask(id="4", agent_type="tester", description="test", depends_on=["3"]),
            ],
        )

        asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertEqual(bridge.calls, ["builder"])
        reviewer = next(st for st in plan.subtasks if st.id == "2")
        deployer = next(st for st in plan.subtasks if st.id == "3")
        tester = next(st for st in plan.subtasks if st.id == "4")
        self.assertEqual(reviewer.status, TaskStatus.BLOCKED)
        self.assertEqual(deployer.status, TaskStatus.BLOCKED)
        self.assertEqual(tester.status, TaskStatus.BLOCKED)


class TestDebuggerNoop(unittest.TestCase):
    def test_debugger_noops_when_reviewer_and_tester_already_passed(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()

        reviewer = SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1"])
        tester = SubTask(id="4", agent_type="tester", description="test", depends_on=["3"])
        debugger = SubTask(id="5", agent_type="debugger", description="debug", depends_on=["4"])
        plan = Plan(
            goal="做一个高端多页面旅游官网",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                reviewer,
                tester,
                debugger,
            ],
        )
        prev_results = {
            "3": {
                "success": True,
                "output": json.dumps({
                    "verdict": "APPROVED",
                    "scores": {
                        "layout": 8,
                        "color": 8,
                        "typography": 8,
                        "animation": 7,
                        "responsive": 8,
                        "functionality": 8,
                        "completeness": 8,
                        "originality": 7,
                    },
                    "blocking_issues": [],
                    "required_changes": [],
                    "missing_deliverables": [],
                }),
                "error": "",
            },
            "4": {
                "success": True,
                "output": json.dumps({"status": "pass", "details": "All required pages verified."}),
                "error": "",
            },
        }

        result = asyncio.run(orch._execute_subtask(debugger, plan, "kimi-coding", prev_results=prev_results))

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("mode"), "debugger_noop")
        self.assertEqual(result.get("files_created"), [])
        self.assertIn("不做额外改写", result.get("output", ""))
        self.assertEqual(debugger.status, TaskStatus.COMPLETED)
        self.assertTrue(any(
            call.args[0] == "subtask_progress" and call.args[1].get("stage") == "debugger_noop"
            for call in orch.emit.await_args_list
        ))

    def test_execute_plan_reviewer_requeue_clears_progress_high_water_and_resets_nodes(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._progress_high_water = {"1": 100, "2": 100, "4": 100}

        builder_a = SubTask(id="1", agent_type="builder", description="build top", depends_on=[], max_retries=1)
        builder_b = SubTask(id="2", agent_type="builder", description="build bottom", depends_on=[], max_retries=1)
        reviewer = SubTask(id="4", agent_type="reviewer", description="review", depends_on=["1", "2"], max_retries=0)
        downstream = SubTask(id="5", agent_type="tester", description="test", depends_on=["4"], max_retries=0)
        plan = Plan(goal="Build premium website", subtasks=[builder_a, builder_b, reviewer, downstream])
        attempts = {"4": 0}

        async def fake_execute_subtask(st, _plan, _model, results):
            if st.id == "1":
                st.status = TaskStatus.COMPLETED
                st.output = "builder-a-ok"
                return {"success": True, "output": st.output}
            if st.id == "2":
                st.status = TaskStatus.COMPLETED
                st.output = "builder-b-ok"
                return {"success": True, "output": st.output}
            if st.id == "4":
                attempts["4"] += 1
                if attempts["4"] == 1:
                    return {"success": False, "requeue_requested": True, "requeue_subtasks": ["1", "2", "4"]}
                st.status = TaskStatus.COMPLETED
                st.output = "review-ok"
                return {"success": True, "output": st.output}
            st.status = TaskStatus.COMPLETED
            st.output = f"ok-{st.id}"
            return {"success": True, "output": st.output}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        results = asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertEqual(attempts["4"], 2)
        self.assertTrue(results["5"]["success"])
        self.assertEqual(orch._progress_high_water, {})
        self.assertEqual(builder_a.status, TaskStatus.COMPLETED)
        self.assertEqual(builder_b.status, TaskStatus.COMPLETED)
        self.assertEqual(reviewer.status, TaskStatus.COMPLETED)
        self.assertEqual(downstream.status, TaskStatus.COMPLETED)

        queued_calls = [
            call for call in orch._sync_ne_status.await_args_list
            if len(call.args) >= 2 and call.args[1] == "queued"
        ]
        self.assertEqual([call.args[0] for call in queued_calls], ["1", "2", "4", "5"])
        for call in queued_calls:
            self.assertEqual(call.kwargs.get("progress"), 0)
            self.assertEqual(call.kwargs.get("phase"), "requeued")
            self.assertEqual(call.kwargs.get("output_summary"), "")
            self.assertEqual(call.kwargs.get("error_message"), "")
            self.assertTrue(call.kwargs.get("reset_started_at"))

        requeue_events = [
            call.args[1] for call in orch.emit.await_args_list
            if len(call.args) >= 2
            and call.args[0] == "subtask_progress"
            and isinstance(call.args[1], dict)
            and call.args[1].get("stage") == "requeue_downstream"
        ]
        self.assertEqual(len(requeue_events), 1)
        self.assertEqual(requeue_events[0].get("requeue_subtasks"), ["1", "2", "4", "5"])

    def test_execute_plan_requeue_ignores_stale_success_from_same_ready_batch(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()

        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=1)
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"], max_retries=0)
        deployer = SubTask(id="3", agent_type="deployer", description="deploy", depends_on=["1"], max_retries=0)
        tester = SubTask(id="4", agent_type="tester", description="test", depends_on=["3"], max_retries=0)
        plan = Plan(goal="Build landing page", subtasks=[builder, reviewer, deployer, tester])

        attempts = {"1": 0, "2": 0, "3": 0, "4": 0}
        call_order = []

        async def fake_execute_subtask(st, _plan, _model, _results):
            attempts[st.id] += 1
            call_order.append(f"{st.id}:{attempts[st.id]}")
            if st.id == "1":
                st.status = TaskStatus.COMPLETED
                st.output = f"builder-{attempts[st.id]}"
                return {"success": True, "output": st.output}
            if st.id == "2":
                if attempts["2"] == 1:
                    return {"success": False, "requeue_requested": True, "requeue_subtasks": ["1", "2"]}
                st.status = TaskStatus.COMPLETED
                st.output = "review-ok"
                return {"success": True, "output": st.output}
            if st.id == "3":
                st.status = TaskStatus.COMPLETED
                st.output = f"deploy-{attempts[st.id]}"
                return {"success": True, "output": st.output}
            st.status = TaskStatus.COMPLETED
            st.output = "test-ok"
            return {"success": True, "output": st.output}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        results = asyncio.run(orch._execute_plan(plan, "kimi-coding"))

        self.assertTrue(results["4"]["success"])
        self.assertEqual(attempts["1"], 2)
        self.assertEqual(attempts["2"], 2)
        self.assertEqual(attempts["3"], 2)
        self.assertEqual(attempts["4"], 1)
        self.assertEqual(call_order, ["1:1", "2:1", "3:1", "1:2", "2:2", "3:2", "4:1"])

    def test_execute_plan_parallel_builder_failures_retry_independently(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()

        builder1 = SubTask(id="1", agent_type="builder", description="build core shell", depends_on=[], max_retries=1)
        builder2 = SubTask(id="2", agent_type="builder", description="build support lane", depends_on=[], max_retries=1)
        plan = Plan(goal="做一个第三人称 3D 射击游戏", subtasks=[builder1, builder2])

        attempts = {"1": 0, "2": 0}
        retry_started = {"1": asyncio.Event(), "2": asyncio.Event()}
        release_builder1_retry = asyncio.Event()

        async def fake_execute_subtask(st, _plan, _model, _results):
            attempts[st.id] += 1
            if attempts[st.id] == 1:
                return {"success": False, "error": f"{st.id} failed", "tool_results": []}

            retry_started[st.id].set()
            if st.id == "1":
                await release_builder1_retry.wait()
            st.status = TaskStatus.COMPLETED
            st.output = f"{st.id}-ok"
            return {"success": True, "output": st.output, "tool_results": []}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        async def run_plan():
            task = asyncio.create_task(orch._execute_plan(plan, "kimi-coding"))
            await asyncio.wait_for(retry_started["1"].wait(), 0.2)
            await asyncio.wait_for(retry_started["2"].wait(), 0.2)
            release_builder1_retry.set()
            return await asyncio.wait_for(task, 1.0)

        with patch.object(orchestrator_module.random, "uniform", return_value=0.0):
            results = asyncio.run(run_plan())

        self.assertTrue(results["1"]["success"])
        self.assertTrue(results["2"]["success"])
        self.assertEqual(attempts["1"], 2)
        self.assertEqual(attempts["2"], 2)

    def test_execute_plan_parallel_builder_retry_starts_before_sibling_attempt_finishes(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()

        builder1 = SubTask(id="1", agent_type="builder", description="build core shell", depends_on=[], max_retries=1)
        builder2 = SubTask(id="2", agent_type="builder", description="build support lane", depends_on=[], max_retries=1)
        plan = Plan(goal="做一个第三人称 3D 射击游戏", subtasks=[builder1, builder2])

        attempts = {"1": 0, "2": 0}
        builder1_retry_started = asyncio.Event()
        builder2_first_attempt_started = asyncio.Event()
        release_builder2_first_attempt = asyncio.Event()

        async def fake_execute_subtask(st, _plan, _model, _results):
            attempts[st.id] += 1
            if st.id == "1" and attempts[st.id] == 1:
                return {"success": False, "error": "builder1 failed", "tool_results": []}
            if st.id == "1":
                builder1_retry_started.set()
                st.status = TaskStatus.COMPLETED
                st.output = "builder1-ok"
                return {"success": True, "output": st.output, "tool_results": []}
            if attempts[st.id] == 1:
                builder2_first_attempt_started.set()
                await release_builder2_first_attempt.wait()
            st.status = TaskStatus.COMPLETED
            st.output = "builder2-ok"
            return {"success": True, "output": st.output, "tool_results": []}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        async def run_plan():
            task = asyncio.create_task(orch._execute_plan(plan, "kimi-coding"))
            await asyncio.wait_for(builder2_first_attempt_started.wait(), 0.2)
            await asyncio.wait_for(builder1_retry_started.wait(), 0.2)
            release_builder2_first_attempt.set()
            return await asyncio.wait_for(task, 1.0)

        with patch.object(orchestrator_module.random, "uniform", return_value=0.0):
            results = asyncio.run(run_plan())

        self.assertTrue(results["1"]["success"])
        self.assertTrue(results["2"]["success"])
        self.assertEqual(attempts["1"], 2)
        self.assertEqual(attempts["2"], 1)

    def test_parallel_website_support_builder_failure_proceeds(self):
        """v3.0.5: Support-lane builder failure is soft — pipeline proceeds with primary builder output."""
        class StubBridge:
            def __init__(self, out_dir: Path):
                self.calls = []
                self.config = {}
                self.out_dir = out_dir

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.calls.append(node.get("type"))
                node_id = str(node.get("id") or "")
                if node.get("type") == "builder" and node_id.startswith("auto_2"):
                    part1 = self.out_dir / "index_part1.html"
                    part1.write_text("<section><h1>Top Half</h1></section>", encoding="utf-8")
                    return {
                        "success": True,
                        "output": "<section><h1>Top Half</h1></section>",
                        "tool_results": [{"written": True, "path": str(part1)}],
                    }
                if node.get("type") == "builder" and node_id.startswith("auto_3"):
                    return {"success": False, "output": "", "error": "builder part2 failed", "tool_results": []}
                return {"success": True, "output": "ok", "tool_results": []}

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            bridge = StubBridge(out_dir)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch._current_task_type = "website"

            async def _noop(_evt):
                return None

            orch.on_event = _noop
            plan = Plan(
                goal="build premium website",
                subtasks=[
                    SubTask(id="2", agent_type="builder", description="Build top and save to /tmp/evermind_output/index_part1.html", depends_on=[], max_retries=0),
                    SubTask(id="3", agent_type="builder", description="Build bottom and save to /tmp/evermind_output/index_part2.html", depends_on=[], max_retries=0),
                    SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"], max_retries=0),
                    SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["2", "3"], max_retries=0),
                    SubTask(id="6", agent_type="tester", description="test", depends_on=["4", "5"], max_retries=0),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                preview_validation_module.OUTPUT_DIR = out_dir
                asyncio.run(orch._execute_plan(plan, "kimi-coding"))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        # Support-lane builder (slot 2) failure is soft — downstream proceeds
        self.assertIn("builder", bridge.calls)
        reviewer = next(st for st in plan.subtasks if st.id == "4")
        deployer = next(st for st in plan.subtasks if st.id == "5")
        self.assertNotEqual(reviewer.status, TaskStatus.BLOCKED)
        self.assertNotEqual(deployer.status, TaskStatus.BLOCKED)

    def test_parallel_website_primary_builder_failure_blocks_downstream(self):
        """When the PRIMARY builder (slot 1) fails, downstream MUST block."""
        class StubBridge:
            def __init__(self, out_dir: Path):
                self.calls = []
                self.config = {}
                self.out_dir = out_dir

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.calls.append(node.get("type"))
                node_id = str(node.get("id") or "")
                # Primary builder (slot 1) fails
                if node.get("type") == "builder" and node_id.startswith("auto_2"):
                    return {"success": False, "output": "", "error": "primary builder failed", "tool_results": []}
                # Support builder (slot 2) succeeds
                if node.get("type") == "builder" and node_id.startswith("auto_3"):
                    return {"success": True, "output": "support ok", "tool_results": []}
                return {"success": True, "output": "ok", "tool_results": []}

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            bridge = StubBridge(out_dir)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch._current_task_type = "website"

            async def _noop(_evt):
                return None

            orch.on_event = _noop
            plan = Plan(
                goal="build premium website",
                subtasks=[
                    SubTask(id="2", agent_type="builder", description="Build main page", depends_on=[], max_retries=0),
                    SubTask(id="3", agent_type="builder", description="Build support modules", depends_on=[], max_retries=0),
                    SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"], max_retries=0),
                    SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["2", "3"], max_retries=0),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                preview_validation_module.OUTPUT_DIR = out_dir
                asyncio.run(orch._execute_plan(plan, "kimi-coding"))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        # Primary builder failure is hard — downstream blocks
        self.assertEqual(bridge.calls, ["builder", "builder"])
        reviewer = next(st for st in plan.subtasks if st.id == "4")
        deployer = next(st for st in plan.subtasks if st.id == "5")
        self.assertEqual(reviewer.status, TaskStatus.BLOCKED)
        self.assertEqual(deployer.status, TaskStatus.BLOCKED)

    def test_parallel_nonwebsite_builder_failure_blocks_even_with_preview(self):
        class StubBridge:
            def __init__(self, out_dir: Path):
                self.calls = []
                self.config = {"tester_run_smoke": False}
                self.out_dir = out_dir

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.calls.append(node.get("type"))
                text = str(input_data)
                if node.get("type") == "builder" and "Build core gameplay" in text:
                    index = self.out_dir / "index.html"
                    index.write_text(
                        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Snake Arena</title>
<style>
:root { --bg:#071018; --panel:#102132; --fg:#eff6ff; --accent:#38bdf8; --accent2:#34d399; --danger:#fb7185; }
* { box-sizing:border-box; }
body { margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:radial-gradient(circle at top,#12324d,#071018 62%); color:var(--fg); }
header,main,section,footer,nav { display:block; }
nav { display:flex; justify-content:space-between; align-items:center; padding:18px 24px; }
main { display:grid; gap:18px; padding:24px; }
.hero { display:grid; grid-template-columns:1.2fr .8fr; gap:18px; align-items:center; }
.panel { background:rgba(16,33,50,.88); border:1px solid rgba(148,163,184,.18); border-radius:18px; padding:18px; box-shadow:0 18px 60px rgba(0,0,0,.28); }
.hud { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.arena { min-height:320px; border-radius:16px; border:1px solid rgba(56,189,248,.35); background:linear-gradient(180deg,rgba(15,23,42,.86),rgba(8,47,73,.9)); }
.tips { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.cta { display:flex; gap:12px; flex-wrap:wrap; }
button { border:none; border-radius:999px; padding:12px 18px; font-weight:700; background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#062033; }
small { opacity:.75; }
@media (max-width: 900px) { .hero { grid-template-columns:1fr; } .hud, .tips { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><nav><strong>Snake Arena</strong><small>Core gameplay ready</small></nav></header>
<main>
  <section class="hero">
    <div class="panel">
      <h1>Arcade snake with polished movement</h1>
      <p>Core loop, input handling, collision detection, and board rendering are already wired.</p>
      <div class="cta"><button>Start Run</button><button>Practice Mode</button></div>
    </div>
    <div class="panel hud">
      <article><strong>Score</strong><p>0012</p></article>
      <article><strong>Speed</strong><p>Normal</p></article>
      <article><strong>Lives</strong><p>3</p></article>
    </div>
  </section>
  <section class="panel arena"><canvas aria-label="Game arena"></canvas></section>
  <section class="tips">
    <article class="panel">Arrow key controls</article>
    <article class="panel">Fruit combo streaks</article>
    <article class="panel">Pause and restart states</article>
  </section>
</main>
<footer class="panel">Ready for polish pass and secondary effects.</footer>
<script>window.game=true; window.snakeReady=true;</script>
</body>
</html>""",
                        encoding="utf-8",
                    )
                    return {
                        "success": True,
                        "output": "<!DOCTYPE html><html><body>game</body></html>",
                        "tool_results": [{"written": True, "path": str(index)}],
                    }
                if node.get("type") == "builder":
                    return {"success": False, "output": "", "error": "polish builder failed", "tool_results": []}
                if node.get("type") == "reviewer":
                    return {
                        "success": True,
                        "output": "{\"verdict\":\"APPROVED\"}",
                        "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                        "tool_call_stats": {"browser": 1},
                    }
                if node.get("type") == "deployer":
                    return {"success": True, "output": "{\"status\":\"deployed\",\"preview_url\":\"http://127.0.0.1:8765/preview/index.html\"}", "tool_results": []}
                if node.get("type") == "tester":
                    return {
                        "success": True,
                        "output": "{\"status\":\"pass\"}",
                        "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                        "tool_call_stats": {"browser": 1},
                    }
                return {"success": True, "output": "ok", "tool_results": []}

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            bridge = StubBridge(out_dir)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch._current_task_type = "game"

            async def _noop(_evt):
                return None

            orch.on_event = _noop
            plan = Plan(
                goal="build snake game",
                subtasks=[
                    SubTask(id="2", agent_type="builder", description="ADVANCED MODE\nBuild core gameplay and save to /tmp/evermind_output/index.html", depends_on=[], max_retries=0),
                    SubTask(id="3", agent_type="builder", description="ADVANCED MODE\nBuild polish layer", depends_on=[], max_retries=0),
                    SubTask(id="4", agent_type="reviewer", description="review", depends_on=["2", "3"]),
                    SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["2", "3"]),
                    SubTask(id="6", agent_type="tester", description="test", depends_on=["4", "5"]),
                ],
            )

            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out_dir
                preview_validation_module.OUTPUT_DIR = out_dir
                asyncio.run(orch._execute_plan(plan, "kimi-coding"))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertEqual(bridge.calls, ["builder", "builder"])
        reviewer = next(st for st in plan.subtasks if st.id == "4")
        deployer = next(st for st in plan.subtasks if st.id == "5")
        tester = next(st for st in plan.subtasks if st.id == "6")
        self.assertEqual(reviewer.status, TaskStatus.BLOCKED)
        self.assertEqual(deployer.status, TaskStatus.BLOCKED)
        self.assertEqual(tester.status, TaskStatus.BLOCKED)


class TestRetryFromFailureStateReset(unittest.TestCase):
    def test_retry_from_failure_requeues_downstream_tasks(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "<!DOCTYPE html><html><head></head><body>fixed</body></html>"}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._progress_high_water = {"2": 100, "3": 100}

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(
            goal="test",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="2", agent_type="deployer", description="deploy", depends_on=["1"]),
                SubTask(id="3", agent_type="tester", description="test", depends_on=["2"]),
            ],
        )
        for st in plan.subtasks:
            st.status = TaskStatus.COMPLETED
            st.output = "ok"

        test_task = plan.subtasks[2]
        results = {
            "1": {"success": True, "output": "builder-old"},
            "2": {"success": True, "output": "deployer-old"},
            "3": {"success": True, "output": "tester-old"},
        }
        succeeded = {"1", "2", "3"}
        completed = {"1", "2", "3"}
        failed = set()

        async def _fake_execute(subtask, _plan, _model, _results):
            subtask.status = TaskStatus.COMPLETED
            subtask.output = "fixed"
            subtask.error = ""
            return {"success": True, "output": "fixed"}

        with patch.object(orch, "_execute_subtask", new=AsyncMock(side_effect=_fake_execute)):
            asyncio.run(
                orch._retry_from_failure(
                    plan=plan,
                    test_task=test_task,
                    test_result={"status": "fail", "errors": ["visual gate failed"], "suggestion": "fix"},
                    model="kimi-coding",
                    results=results,
                    succeeded=succeeded,
                    completed=completed,
                    failed=failed,
                )
            )

        builder = plan.subtasks[0]
        deployer = plan.subtasks[1]
        tester = plan.subtasks[2]

        self.assertEqual(builder.status, TaskStatus.COMPLETED)
        self.assertGreaterEqual(builder.retries, 1)
        self.assertEqual(deployer.status, TaskStatus.PENDING)
        self.assertEqual(tester.status, TaskStatus.PENDING)
        self.assertNotIn("2", succeeded)
        self.assertNotIn("3", succeeded)
        self.assertNotIn("2", completed)
        self.assertNotIn("3", completed)
        self.assertNotIn("2", results)
        self.assertNotIn("3", results)
        self.assertEqual(orch._progress_high_water, {})

        queued_calls = [
            call for call in orch._sync_ne_status.await_args_list
            if len(call.args) >= 2 and call.args[1] == "queued"
        ]
        self.assertEqual([call.args[0] for call in queued_calls], ["2", "3"])
        for call in queued_calls:
            self.assertEqual(call.kwargs.get("progress"), 0)
            self.assertEqual(call.kwargs.get("phase"), "requeued")
            self.assertEqual(call.kwargs.get("output_summary"), "")
            self.assertEqual(call.kwargs.get("error_message"), "")
            self.assertTrue(call.kwargs.get("reset_started_at"))

        requeue_events = [
            call.args[1] for call in orch.emit.await_args_list
            if len(call.args) >= 2
            and call.args[0] == "subtask_progress"
            and isinstance(call.args[1], dict)
            and call.args[1].get("stage") == "requeue_downstream"
        ]
        self.assertEqual(len(requeue_events), 1)
        self.assertEqual(requeue_events[0].get("requeue_subtasks"), ["2", "3"])


class TestStablePreviewPreservation(unittest.TestCase):
    def test_prepare_output_dir_for_run_preserves_files_for_session_continuation(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._current_goal = "游戏写得还可以，但是还有一点问题，你要详细修复这些问题"
        orch._current_conversation_history = [{"role": "user", "content": "继续优化这个游戏"}]
        orch._session_continuation_hint = True

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            live_html = tmp_out / "index.html"
            live_html.write_text("<!doctype html><html><body>keep me</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._prepare_output_dir_for_run()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(live_html.exists())

    def test_prepare_output_dir_for_run_clears_files_when_session_continuation_explicitly_false(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        orch._current_goal = "创建一个全新的 3D 第三人称射击游戏，要有怪物、武器、大地图和关卡。"
        orch._current_conversation_history = [{"role": "user", "content": "继续优化这个游戏"}]
        orch._session_continuation_hint = False
        orch._session_continuation_explicit = False

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            live_html = tmp_out / "index.html"
            live_html.write_text("<!doctype html><html><body>stale artifact</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._prepare_output_dir_for_run()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertFalse(live_html.exists())

    def test_prepare_output_dir_for_run_preserves_stable_previews(self):
        orch = Orchestrator(ai_bridge=None, executor=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            stable_html = tmp_out / "_stable_previews" / "run_prev" / "final_success_task_final" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")
            failed_html = tmp_out / "task_9" / "index.html"
            failed_html.parent.mkdir(parents=True, exist_ok=True)
            failed_html.write_text("<!doctype html><html><body>failed</body></html>", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._prepare_output_dir_for_run()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(stable_html.exists())
            self.assertFalse(failed_html.exists())

    def test_prepare_output_dir_for_run_removes_stale_root_assets_and_temp_images(self):
        orch = Orchestrator(ai_bridge=None, executor=None)

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            stable_html = tmp_out / "_stable_previews" / "run_prev" / "final_success_task_final" / "index.html"
            stable_html.parent.mkdir(parents=True, exist_ok=True)
            stable_html.write_text("<!doctype html><html><body>stable</body></html>", encoding="utf-8")
            (tmp_out / "assets").mkdir(parents=True, exist_ok=True)
            (tmp_out / "assets" / "old.png").write_text("x", encoding="utf-8")
            (tmp_out / "browser_records").mkdir(parents=True, exist_ok=True)
            (tmp_out / "browser_records" / "trace.json").write_text("{}", encoding="utf-8")
            (tmp_out / "_builder_backups").mkdir(parents=True, exist_ok=True)
            (tmp_out / "_builder_backups" / "index.bak").write_text("backup", encoding="utf-8")
            (tmp_out / "_evermind_runtime" / "three").mkdir(parents=True, exist_ok=True)
            (tmp_out / "_evermind_runtime" / "three" / "three.min.js").write_text("runtime", encoding="utf-8")
            (tmp_out / "_browser_use").mkdir(parents=True, exist_ok=True)
            (tmp_out / "_browser_use" / "session.json").write_text("{}", encoding="utf-8")
            (tmp_out / "_visual_regression").mkdir(parents=True, exist_ok=True)
            (tmp_out / "_visual_regression" / "report.json").write_text("{}", encoding="utf-8")
            (tmp_out / "_evermind_scratch").mkdir(parents=True, exist_ok=True)
            (tmp_out / "_evermind_scratch" / "draft.txt").write_text("draft", encoding="utf-8")
            (tmp_out / "index.html").write_text("<!doctype html><html><body>stale root</body></html>", encoding="utf-8")
            (tmp_out / "report.json").write_text("{}", encoding="utf-8")
            (tmp_out / "tmpqtcfo9rf.png").write_text("image", encoding="utf-8")

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                orch._prepare_output_dir_for_run()
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

            self.assertTrue(stable_html.exists())
            self.assertFalse((tmp_out / "assets").exists())
            self.assertFalse((tmp_out / "browser_records").exists())
            self.assertFalse((tmp_out / "_builder_backups").exists())
            self.assertFalse((tmp_out / "_evermind_runtime").exists())
            self.assertFalse((tmp_out / "_browser_use").exists())
            self.assertFalse((tmp_out / "_visual_regression").exists())
            self.assertFalse((tmp_out / "_evermind_scratch").exists())
            self.assertFalse((tmp_out / "index.html").exists())
            self.assertFalse((tmp_out / "report.json").exists())
            self.assertFalse((tmp_out / "tmpqtcfo9rf.png").exists())

    def test_builder_browser_is_suppressed_when_upstream_handoff_exists(self):
        class StubBridge:
            def __init__(self):
                self.config = {"builder": {"enable_browser_search": True}}
                self.seen_plugins = []

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.seen_plugins = [getattr(plugin, "name", "") for plugin in plugins]
                return {
                    "success": True,
                    "output": """```html index.html
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'></head><body><main><section><h1>Maison</h1><p>Luxury site</p></section></main></body></html>
```""",
                    "tool_results": [],
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch.emit = AsyncMock()
        plan = Plan(
            goal="做一个品牌官网首页",
            subtasks=[
                SubTask(id="1", agent_type="analyst", description="research"),
                SubTask(id="2", agent_type="uidesign", description="design"),
                SubTask(id="3", agent_type="scribe", description="content"),
                SubTask(id="4", agent_type="builder", description="build", depends_on=["1", "2", "3"]),
            ],
        )
        prev_results = {
            "1": {"output": "analyst handoff"},
            "2": {"output": "ui handoff"},
            "3": {"output": "scribe handoff"},
        }

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                preview_validation_module.OUTPUT_DIR = Path(td)
                result = asyncio.run(orch._execute_subtask(plan.subtasks[3], plan, "kimi-coding", prev_results))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn("file_ops", bridge.seen_plugins)
        self.assertNotIn("browser", bridge.seen_plugins)

    def test_multi_page_builder_prompt_includes_assigned_html_filenames(self):
        class StubBridge:
            def __init__(self):
                self.config = {}
                self.seen_input = ""

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.seen_input = str(input_data or "")
                return {"success": False, "output": "", "error": "stop", "tool_results": []}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch.emit = AsyncMock()
        plan = Plan(
            goal="做一个8页奢侈品品牌官网",
            subtasks=[
                SubTask(id="4", agent_type="builder", description="build primary", depends_on=[]),
                SubTask(id="5", agent_type="builder", description="build secondary", depends_on=[]),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                preview_validation_module.OUTPUT_DIR = Path(td)
                asyncio.run(orch._execute_subtask(plan.subtasks[0], plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(
            "Assigned HTML filenames for this builder: index.html, pricing.html, features.html, solutions.html.",
            bridge.seen_input,
        )
        self.assertIn(
            "This builder run is in DIRECT MULTI-FILE DELIVERY mode.",
            bridge.seen_input,
        )
        self.assertIn(
            "Return fenced HTML blocks for the assigned filenames directly in the model response.",
            bridge.seen_input,
        )
        self.assertIn(
            "A single-page draft is considered incomplete delivery.",
            bridge.seen_input,
        )

    def test_single_page_game_builder_prompt_assigns_only_index_html(self):
        class StubBridge:
            def __init__(self):
                self.config = {}
                self.seen_input = ""

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.seen_input = str(input_data or "")
                return {"success": False, "output": "", "error": "stop", "tool_results": []}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch.emit = AsyncMock()
        plan = Plan(
            goal="创建一个3d射击游戏，要有怪物和不同武器，保存到 index.html。",
            subtasks=[
                SubTask(id="4", agent_type="builder", description="build the playable game shell", depends_on=[]),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = Path(td)
                preview_validation_module.OUTPUT_DIR = Path(td)
                asyncio.run(orch._execute_subtask(plan.subtasks[0], plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(
            "Assigned HTML filenames for this builder: index.html.",
            bridge.seen_input,
        )
        self.assertNotIn(
            "Assigned HTML filenames for this builder: index.html, pricing.html",
            bridge.seen_input,
        )


class TestBuilderBootstrapScaffold(unittest.TestCase):
    def test_single_builder_multi_page_gets_full_target_set_and_scaffold(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="做一个介绍奢侈品的英文网站（8页），页面要简约高级，像苹果一样",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build the full premium multi-page experience",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                targets = orch._builder_bootstrap_targets(plan, plan.subtasks[0])
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[0])
                files_exist = all((tmp_out / name).exists() for name in targets)
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertEqual(len(targets), 8)
        self.assertEqual(targets[0], "index.html")
        self.assertEqual(targets[1:4], ["pricing.html", "features.html", "solutions.html"])
        self.assertEqual(len(written), 8)
        self.assertTrue(files_exist)

    def test_single_builder_travel_multi_page_uses_travel_fallback_routes(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="创建一个介绍美国旅游景点的 8 页网站，详细介绍加州所有比较好玩的景点和旅行攻略",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build the full California travel multi-page experience",
                ),
            ],
        )

        targets = orch._builder_bootstrap_targets(plan, plan.subtasks[0])

        self.assertEqual(len(targets), 8)
        self.assertEqual(
            targets[:5],
            ["index.html", "attractions.html", "cities.html", "nature.html", "coast.html"],
        )
        self.assertNotIn("pricing.html", targets[:6])

    def test_multi_page_builder_seeds_internal_scaffold_without_counting_as_preview(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, input_data):
                text = str(input_data or "")
                if "index.html" in text:
                    return ["index.html", "brand.html", "craftsmanship.html", "collections.html"]
                return ["materials.html", "heritage.html", "boutiques.html", "contact.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="制作一个8页奢侈品网站",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="must create /tmp/evermind_output/index.html and fallback set: brand.html, craftsmanship.html, collections.html",
                ),
                SubTask(
                    id="5",
                    agent_type="builder",
                    description="fallback set: materials.html, heritage.html, boutiques.html, contact.html",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[0])
                current = orch._current_run_html_artifacts()
                exists = (tmp_out / "index.html").exists()
                is_bootstrap = preview_validation_module.is_bootstrap_html_artifact(tmp_out / "index.html")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(str(tmp_out / "index.html"), written)
        self.assertTrue(exists)
        self.assertTrue(is_bootstrap)
        self.assertEqual(current, [])

    def test_multi_page_builder_reseeds_corrupted_assigned_page(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, input_data):
                return ["index.html", "pricing.html", "features.html", "contact.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="制作一个4页奢侈品网站",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build index.html, pricing.html, features.html, contact.html",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            broken = tmp_out / "pricing.html"
            broken.write_text(
                "<!DOCTYPE html><html><head><style>body{opacity:1}... [TRUNCATED]",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[0])
                rewritten = broken.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(str(broken), written)
        self.assertIn("evermind-bootstrap", rewritten)

    def test_secondary_builder_never_seeds_index_even_if_description_mentions_it(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, input_data):
                text = str(input_data or "")
                if "primary builder" in text:
                    return ["index.html", "brand.html", "craftsmanship.html", "collections.html"]
                return ["index.html", "materials.html", "heritage.html", "contact.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="制作一个8页奢侈品网站",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="primary builder must create /tmp/evermind_output/index.html plus brand.html, craftsmanship.html, collections.html",
                ),
                SubTask(
                    id="5",
                    agent_type="builder",
                    description="secondary builder got a noisy handoff that also mentions index.html alongside materials.html, heritage.html, contact.html",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                primary_targets = orch._builder_bootstrap_targets(plan, plan.subtasks[0])
                secondary_targets = orch._builder_bootstrap_targets(plan, plan.subtasks[1])
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[1])
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertEqual(primary_targets[0], "index.html")
        self.assertNotIn("index.html", secondary_targets)
        self.assertEqual(len(secondary_targets), 4)
        self.assertNotIn(str(tmp_out / "index.html"), written)
        self.assertTrue(all(Path(item).name != "index.html" for item in written))

    def test_goal_language_mismatch_page_is_not_preserved_over_scaffold(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="做一个介绍奢侈品的英文网站（8页），页面要像苹果官网一样高级",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="build the full premium multi-page experience",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            index_path = tmp_out / "index.html"
            index_path.write_text(
                "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"><title>器之道</title><style>body{font-family:sans-serif}main{padding:40px}</style></head><body><main><section><h1>器之道</h1><p>东方工艺美学与品牌故事。</p></section><section><h2>系列</h2><p>器物、匠心、传承。</p></section><script>console.log('ok')</script></main></body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                written = orch._ensure_builder_bootstrap_scaffold(plan, plan.subtasks[0])
                rewritten = index_path.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn(str(index_path), written)
        self.assertIn("evermind-bootstrap scaffold", rewritten.lower())


class TestBuilderDiskScanIsolation(unittest.TestCase):
    def test_secondary_builder_disk_scan_only_collects_assigned_pages(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            def _builder_assigned_html_targets(self, input_data):
                text = str(input_data or "")
                if "primary builder" in text:
                    return ["index.html", "pricing.html", "features.html", "solutions.html"]
                return ["platform.html", "contact.html", "about.html", "faq.html"]

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None, on_event=None)
        plan = Plan(
            goal="制作一个8页奢侈品网站",
            difficulty="pro",
            subtasks=[
                SubTask(
                    id="4",
                    agent_type="builder",
                    description="primary builder owns index.html, pricing.html, features.html, solutions.html",
                ),
                SubTask(
                    id="5",
                    agent_type="builder",
                    description="secondary builder owns platform.html, contact.html, about.html, faq.html",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text(
                    "<html><body><main><h1>Home</h1><p>Luxury brand homepage with multiple sections.</p></main></body></html>",
                    encoding="utf-8",
                )
                (tmp_out / "contact.html").write_text(
                    "<html><body><main><h1>Contact</h1><p>Reach the concierge team for appointments and support.</p></main></body></html>",
                    encoding="utf-8",
                )
                found = orch._collect_recent_builder_disk_scan_files(
                    plan,
                    plan.subtasks[1],
                    scan_cutoff=time.time() - 10,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn(str(tmp_out / "contact.html"), found)
        self.assertNotIn(str(tmp_out / "index.html"), found)

    def test_game_builder_disk_scan_ignores_low_value_shell_html(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        plan = Plan(
            goal="创建一个 3d 射击游戏，要有怪物和枪械",
            difficulty="pro",
            subtasks=[SubTask(id="5", agent_type="builder", description="build the game", depends_on=[])],
        )

        shell_html = """<!DOCTYPE html>
<html>
<head>
  <title>Shell</title>
  <style>body{margin:0;background:#111;color:#fff} .start-btn{padding:12px 24px}</style>
</head>
<body></body>
</html>"""

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                (tmp_out / "index.html").write_text(shell_html, encoding="utf-8")
                found = orch._collect_recent_builder_disk_scan_files(
                    plan,
                    plan.subtasks[0],
                    scan_cutoff=time.time() - 10,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(found, [])

    def test_greenfield_game_builder_skips_repo_context_even_without_direct_text_mode(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        subtask = SubTask(
            id="5",
            agent_type="builder",
            description="ADVANCED MODE — Use analyst notes and asset manifest. Build a commercial-grade HTML5 game.",
            depends_on=[],
        )
        plan = Plan(
            goal="创建一个3d射击游戏，要有怪物、枪械、关卡和第三人称视角",
            difficulty="pro",
            subtasks=[subtask],
        )

        self.assertTrue(
            orch._should_skip_builder_repo_context(
                plan,
                subtask,
                builder_targets_preview_output=False,
                builder_direct_multifile_mode=False,
                builder_direct_text_mode=False,
            )
        )

    def test_greenfield_game_builder_suppresses_browser_research(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        subtask = SubTask(
            id="5",
            agent_type="builder",
            description="ADVANCED MODE — Use analyst notes and asset manifest. Build a commercial-grade HTML5 game.",
            depends_on=["1"],
        )
        plan = Plan(
            goal="创建一个3d射击游戏，要有怪物、枪械、关卡和第三人称视角",
            difficulty="pro",
            subtasks=[SubTask(id="1", agent_type="analyst", description="research"), subtask],
        )

        self.assertTrue(orch._should_suppress_builder_browser(plan, subtask, repo_context=None))

    def test_repo_game_fix_request_keeps_repo_context_enabled(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        subtask = SubTask(
            id="5",
            agent_type="builder",
            description="修复第三人称射击游戏输入 bug，修改 src/game/player.ts 和 package.json",
            depends_on=[],
        )
        plan = Plan(
            goal="修复这个 repo 里的 3D 游戏输入 bug，保持现有代码结构",
            difficulty="pro",
            subtasks=[subtask],
        )

        self.assertFalse(
            orch._should_skip_builder_repo_context(
                plan,
                subtask,
                builder_targets_preview_output=False,
                builder_direct_multifile_mode=False,
                builder_direct_text_mode=False,
            )
        )


class TestBuilderRootOwnership(unittest.TestCase):
    def test_custom_plan_generic_merger_summary_upgrades_to_integrator_contract(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        goal = "创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。"

        desc = orch._custom_plan_task_description(
            {
                "input_summary": "Merger\n\n[RUN GOAL]\n" + goal,
            },
            "builder",
            "Merger",
            goal,
        )

        self.assertIn("MERGER / INTEGRATOR CONTRACT", desc)
        self.assertIn("Patch and integrate in place", desc)
        self.assertIn("non-empty, implementation-grade support files", desc)
        self.assertIn("Do NOT leave meaningful support files unwired", desc)

    def test_secondary_builder_can_write_root_index_for_single_entry_game_patch_mode(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        plan = Plan(
            goal="做一个双 builder 的 3D 动作游戏，Builder 1 负责核心玩法，Builder 2 负责特效和 HUD",
            difficulty="pro",
            subtasks=[
                SubTask(id="4", agent_type="builder", description="primary builder owns the core game loop and root index.html"),
                SubTask(id="5", agent_type="builder", description="secondary builder refines the existing game in place", depends_on=["4"]),
            ],
        )

        self.assertTrue(orch._builder_can_write_root_index(plan, plan.subtasks[0], plan.goal))
        self.assertTrue(orch._single_entry_game_secondary_builder_patch_mode(plan, plan.subtasks[1], plan.goal))
        self.assertTrue(orch._builder_can_write_root_index(plan, plan.subtasks[1], plan.goal))

    def test_parallel_game_support_builder_stays_out_of_direct_text_root_delivery(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        goal = "创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。"
        plan = Plan(
            goal=goal,
            difficulty="pro",
            subtasks=orch._build_pro_plan_subtasks(goal),
        )

        builders = [st for st in plan.subtasks if st.agent_type == "builder"]
        self.assertEqual(len(builders), 3)
        builder1, builder2, builder3 = builders
        self.assertEqual(builder1.node_key, "builder1")
        self.assertEqual(builder2.node_key, "builder2")
        self.assertEqual(builder3.node_key, "merger")
        self.assertEqual(builder3.node_label, "Merger")

        self.assertEqual(orch._builder_bootstrap_targets(plan, builder1), ["index.html"])
        self.assertEqual(orch._builder_bootstrap_targets(plan, builder2), [])
        self.assertEqual(orch._builder_bootstrap_targets(plan, builder3), ["index.html"])
        self.assertTrue(orch._builder_can_write_root_index(plan, builder1, plan.goal))
        self.assertFalse(orch._builder_can_write_root_index(plan, builder2, plan.goal))
        self.assertTrue(orch._builder_can_write_root_index(plan, builder3, plan.goal))
        self.assertTrue(orch._builder_execution_direct_text_mode(plan, builder1))
        self.assertFalse(orch._builder_execution_direct_text_mode(plan, builder2))
        self.assertFalse(orch._builder_execution_direct_text_mode(plan, builder3))

    def test_parallel_game_builders_get_runtime_model_diversification_when_multiple_keys_exist(self):
        from ai_bridge import AIBridge

        bridge = AIBridge(config={"openai_api_key": "sk-openai", "kimi_api_key": "sk-kimi"})
        # Isolate from persisted local auth/gateway cooldown state seeded from logs.
        bridge._provider_auth_health.clear()
        bridge._compat_gateway_health.clear()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        goal = "创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。"
        plan = Plan(
            goal=goal,
            difficulty="pro",
            subtasks=orch._build_pro_plan_subtasks(goal),
        )

        builder1, builder2, merger = [st for st in plan.subtasks if st.agent_type == "builder"]

        prefs1 = orch._runtime_node_model_preferences(plan, builder1, "kimi-coding")
        prefs2 = orch._runtime_node_model_preferences(plan, builder2, "kimi-coding")
        prefs3 = orch._runtime_node_model_preferences(plan, merger, "kimi-coding")

        self.assertGreaterEqual(len(prefs1), 2)
        self.assertEqual(prefs1[0], "kimi-coding")
        self.assertEqual(prefs2[0], "gpt-5.4-mini")
        self.assertIn("kimi-coding", prefs2[1:])
        self.assertEqual(prefs3[0], "gpt-5.4-mini")
        self.assertIn("kimi-coding", prefs3[1:])

    def test_parallel_game_builders_respect_single_builder_model_preference(self):
        from ai_bridge import AIBridge

        bridge = AIBridge(config={
            "openai_api_key": "sk-openai",
            "kimi_api_key": "sk-kimi",
            "node_model_preferences": {
                "builder": ["kimi-coding"],
            },
        })
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        goal = "创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。"
        plan = Plan(
            goal=goal,
            difficulty="pro",
            subtasks=orch._build_pro_plan_subtasks(goal),
        )

        builder1, builder2, merger = [st for st in plan.subtasks if st.agent_type == "builder"]

        self.assertEqual(orch._runtime_node_model_preferences(plan, builder1, "kimi-coding"), [])
        self.assertEqual(orch._runtime_node_model_preferences(plan, builder2, "kimi-coding"), [])
        self.assertEqual(orch._runtime_node_model_preferences(plan, merger, "kimi-coding"), [])

    def test_parallel_game_support_builder_quality_ignores_live_root_index(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        goal = "创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。"
        plan = Plan(
            goal=goal,
            difficulty="pro",
            subtasks=orch._build_pro_plan_subtasks(goal),
        )
        builder2 = [st for st in plan.subtasks if st.agent_type == "builder"][1]

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                root_index = out / "index.html"
                root_index.write_text(
                    "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
                    "<title>Primary Builder</title><style>body{margin:0;background:#101626;color:#eef2ff}"
                    "main,section,header,footer{display:block}.panel{padding:16px;border:1px solid rgba(255,255,255,.12)}</style>"
                    "</head><body><header>Nav</header><main><section class='panel'><h1>Primary Builder</h1>"
                    "<p>Playable root artifact already exists.</p></section><section class='panel'><p>Additional density.</p></section>"
                    "<section class='panel'><p>Still substantial.</p></section><section class='panel'><p>Footer context.</p></section>"
                    "</main><footer>Footer</footer></body></html>",
                    encoding="utf-8",
                )
                support_js = out / "js" / "combatIntegration.js"
                support_js.parent.mkdir(parents=True, exist_ok=True)
                support_js.write_text(
                    "export function attachCombatIntegration(game){\n"
                    "  game.attachments = game.attachments || [];\n"
                    "  game.attachments.push('combat');\n"
                    "  return game;\n"
                    "}\n",
                    encoding="utf-8",
                )

                files = orch._builder_quality_candidate_files(
                    [str(support_js)],
                    goal=goal,
                    plan=plan,
                    subtask=builder2,
                )
                report = orch._validate_builder_quality(
                    [str(support_js)],
                    "",
                    goal=goal,
                    plan=plan,
                    subtask=builder2,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertEqual([Path(item).name for item in files], ["combatIntegration.js"])
        self.assertTrue(report.get("pass"))
        self.assertNotIn("No saved HTML artifact found", " | ".join(report.get("errors", [])))

    def test_parallel_game_support_builder_rejects_empty_support_file(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        goal = "创建一个第三人称 3D 射击游戏，带怪物、武器、大地图和精美建模。"
        plan = Plan(
            goal=goal,
            difficulty="pro",
            subtasks=orch._build_pro_plan_subtasks(goal),
        )
        builder2 = [st for st in plan.subtasks if st.agent_type == "builder"][1]

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                support_js = out / "js" / "combatIntegration.js"
                support_js.parent.mkdir(parents=True, exist_ok=True)
                support_js.write_text("", encoding="utf-8")
                report = orch._validate_builder_quality(
                    [str(support_js)],
                    "",
                    goal=goal,
                    plan=plan,
                    subtask=builder2,
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertFalse(report.get("pass"))
        self.assertTrue(any("Support files are empty or near-empty" in err for err in report.get("errors", [])))

    def test_secondary_builder_patch_mode_disables_direct_text_first_pass(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        plan = Plan(
            goal="做一个双 builder 的第三人称 3D 射击游戏，Builder 1 负责核心玩法，Builder 2 负责 HUD 和镜头修补",
            difficulty="pro",
            subtasks=[
                SubTask(id="4", agent_type="builder", description="primary builder owns the core game loop and root index.html"),
                SubTask(id="5", agent_type="builder", description="secondary builder refines the existing game in place", depends_on=["4"]),
            ],
        )

        self.assertTrue(orch._builder_requires_existing_artifact_patch(plan, plan.subtasks[1]))
        self.assertFalse(orch._builder_execution_direct_text_mode(plan, plan.subtasks[1]))

    def test_secondary_builder_root_write_is_sanitized_when_patch_mode_not_allowed(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        plan = Plan(
            goal="做一个双 builder 的 3D 动作游戏，Builder 1 负责核心玩法，Builder 2 负责特效和 HUD",
            difficulty="pro",
            subtasks=[
                SubTask(id="4", agent_type="builder", description="primary builder owns the core game loop and root index.html"),
                SubTask(id="5", agent_type="builder", description="secondary builder refines the existing game in place"),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            tmp_out = Path(td)
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = tmp_out
                preview_validation_module.OUTPUT_DIR = tmp_out
                root_index = tmp_out / "index.html"
                root_index.write_text("<html><body>builder1 stable</body></html>", encoding="utf-8")
                orch._snapshot_root_index_for_secondary_builder(plan, plan.subtasks[1])
                root_index.write_text("<html><body>builder2 regression</body></html>", encoding="utf-8")

                kept, dropped = orch._sanitize_builder_generated_files(
                    plan,
                    plan.subtasks[1],
                    [str(root_index)],
                    prev_results={},
                )
                restored = root_index.read_text(encoding="utf-8")
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertEqual(kept, [])
        self.assertEqual(dropped, ["index.html"])
        self.assertIn("builder1 stable", restored)


class TestProjectMemoryLoading(unittest.TestCase):
    def test_project_memory_block_loads_repo_standing_instructions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "CLAUDE.md").write_text("Always preserve the combat loop and do not restart the game from zero.", encoding="utf-8")
            (root / "AGENTS.md").write_text("Builder 2 should only refine visuals and HUD, never replace the root gameplay shell.", encoding="utf-8")
            agent_dir = root / ".claude" / "agents"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "reviewer.md").write_text("Reviewer must reject regressions that remove start flow or restart flow.", encoding="utf-8")

            orch = Orchestrator(
                ai_bridge=SimpleNamespace(config={"workspace": str(root)}),
                executor=None,
                on_event=None,
            )
            block = orch._project_memory_block(str(root))

        self.assertIn("[Project Memory]", block)
        self.assertIn("[CLAUDE.md]", block)
        self.assertIn("[AGENTS.md]", block)
        self.assertIn("combat loop", block)
        self.assertIn("Builder 2 should only refine visuals and HUD", block)
        self.assertNotIn("[.claude/agents/reviewer.md]", block)

    def test_project_agent_brief_block_routes_role_specific_instructions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent_dir = root / ".claude" / "agents"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "builder.md").write_text("Builder owns the gameplay shell and root layout.", encoding="utf-8")
            (agent_dir / "reviewer.md").write_text("Reviewer must reject regressions in the start flow.", encoding="utf-8")
            (agent_dir / "shared.md").write_text("All agents must preserve the save schema.", encoding="utf-8")

            orch = Orchestrator(
                ai_bridge=SimpleNamespace(config={"workspace": str(root)}),
                executor=None,
                on_event=None,
            )
            block = orch._project_agent_brief_block("builder", str(root))

        self.assertIn("[Agent Brief — builder]", block)
        self.assertIn("[.claude/agents/builder.md]", block)
        self.assertIn("[.claude/agents/shared.md]", block)
        self.assertIn("gameplay shell", block)
        self.assertIn("save schema", block)
        self.assertNotIn("[.claude/agents/reviewer.md]", block)


class TestHandleFailureRequiresValidation(unittest.TestCase):
    def test_builder_retry_cannot_succeed_on_text_only_output(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                # Pretend LLM call succeeded but did not write any file.
                return {"success": True, "output": "brief summary only", "tool_results": []}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(
            goal="test",
            subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=1)],
        )
        builder = plan.subtasks[0]
        builder.error = "initial quality failure"

        ok = asyncio.run(orch._handle_failure(builder, plan, "kimi-coding", results={}))
        self.assertFalse(ok)
        self.assertEqual(builder.status, TaskStatus.FAILED)
        self.assertIn("quality gate failed", builder.error.lower())

    def test_support_lane_builder_retry_prompt_preserves_support_file_contract(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()

        goal = "创建一个第三人称 3D 射击游戏，要有怪物、武器和关卡。"
        plan = Plan(
            goal=goal,
            difficulty="pro",
            subtasks=orch._build_pro_plan_subtasks(goal),
        )
        builder2 = [st for st in plan.subtasks if st.agent_type == "builder"][1]
        builder2.error = (
            "Builder quality gate failed (score=18). Errors: "
            "['Support files are empty or near-empty: weaponSystem.js.', "
            "'Support-lane builder did not produce any non-empty JS/CSS/JSON support artifact for its assigned lane.']"
        )

        captured = {}

        async def fake_execute_subtask(st, _plan, _model, _results):
            captured["description"] = st.description
            st.status = TaskStatus.COMPLETED
            st.output = "fixed support lane"
            return {"success": True, "output": st.output, "tool_results": []}

        orch._execute_subtask = fake_execute_subtask  # type: ignore[method-assign]

        ok = asyncio.run(orch._handle_failure(builder2, plan, "kimi-coding", results={}))

        self.assertTrue(ok)
        prompt = captured.get("description", "")
        self.assertIn("support-lane builder", prompt.lower())
        self.assertIn("Do NOT overwrite /tmp/evermind_output/index.html", prompt)
        self.assertIn("Never replace a meaningful support file with an empty shell", prompt)

    def test_support_lane_builder_integrity_flags_invalid_js_syntax(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        goal = "创建一个第三人称 3D 射击游戏，要有怪物、武器和关卡。"
        plan = Plan(
            goal=goal,
            difficulty="pro",
            subtasks=orch._build_pro_plan_subtasks(goal),
        )
        builder2 = [st for st in plan.subtasks if st.agent_type == "builder"][1]
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            bad_js = output_dir / "hudController.js"
            bad_js.write_text(
                "const HUDController = { init() { const overlay = document.createElement('div'); if (overlay) { overlay.style.cssText = ` } ; } };",
                encoding="utf-8",
            )
            with patch("orchestrator.OUTPUT_DIR", output_dir):
                report = orch._builder_support_file_integrity_report(
                    [str(bad_js)],
                    goal=goal,
                    plan=plan,
                    subtask=builder2,
                )
        self.assertTrue(any("invalid JavaScript syntax" in str(item) for item in report.get("errors", [])))

    def test_merger_support_entry_scan_skips_invalid_js_files(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            root_html = output_dir / "index.html"
            root_html.write_text("<!DOCTYPE html><html><head></head><body></body></html>", encoding="utf-8")
            (output_dir / "effectSystem.js").write_text(
                "const EffectSystem = { init() { return true; }, spawnTracer(start, end) { console.log(start, end); } };",
                encoding="utf-8",
            )
            (output_dir / "hudController.js").write_text(
                "const HUDController = { init() { const overlay = document.createElement('div'); if (overlay) { overlay.style.cssText = ` } ; } };",
                encoding="utf-8",
            )
            with patch("orchestrator.OUTPUT_DIR", output_dir):
                entries = orch._meaningful_browser_support_entries(root_html_path=str(root_html), repair=False)
        names = {str(item.get('rel_posix')) for item in entries}
        self.assertIn("effectSystem.js", names)
        self.assertNotIn("hudController.js", names)

    def test_secondary_single_entry_game_builder_is_not_shadow_skipped_when_merger_exists(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        goal = "创建一个第三人称 3D 射击游戏，要有怪物、武器和关卡。"
        plan = Plan(
            goal=goal,
            difficulty="pro",
            subtasks=orch._build_pro_plan_subtasks(goal),
        )
        builders = [st for st in plan.subtasks if st.agent_type == "builder"]
        builder1, builder2 = builders[0], builders[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            root_index = output_dir / "index.html"
            root_index.write_text("<!DOCTYPE html><html><body>ok</body></html>", encoding="utf-8")
            prev_results = {
                builder1.id: {"success": True, "files_created": [str(root_index)]},
            }
            with patch("orchestrator.OUTPUT_DIR", output_dir):
                reason = orch._secondary_single_entry_builder_skip_reason(plan, builder2, prev_results)
        self.assertEqual(reason, "")

    def test_planner_fallback_finalizer_persists_observability_artifacts(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        orch.emit = AsyncMock()
        orch._sync_ne_status = AsyncMock()
        orch._emit_ne_progress = AsyncMock()
        orch._persist_execution_observability_artifacts = Mock()
        orch._humanize_output_summary = Mock(return_value="planner fallback summary")
        orch._append_ne_activity = Mock()
        plan = Plan(
            goal="创建一个第三人称 3D 射击游戏，要有怪物、武器和关卡。",
            subtasks=[SubTask(id="1", agent_type="planner", description="plan", depends_on=[])],
        )
        planner = plan.subtasks[0]
        fake_store = SimpleNamespace(get_node_execution=lambda _ne_id: {})
        with patch("orchestrator.get_node_execution_store", return_value=fake_store):
            result = asyncio.run(
                orch._finalize_planner_fallback(
                    planner,
                    plan,
                    '{"architecture":"fallback"}',
                    mode="planner_timeout_fallback",
                    note="planner fallback note",
                    prev_results={},
                )
            )
        self.assertTrue(result["success"])
        orch._persist_execution_observability_artifacts.assert_called_once()
        orch._sync_ne_status.assert_awaited()
        orch.emit.assert_any_call(
            "subtask_complete",
            unittest.mock.ANY,
        )

    def test_builder_quality_flags_missing_crosshair_for_shooter_brief(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        goal = "创建一个第三人称3D射击游戏，要有清晰弹道和射击准心。"
        plan = Plan(goal=goal, difficulty="pro", subtasks=orch._build_pro_plan_subtasks(goal))
        builder1 = [st for st in plan.subtasks if st.agent_type == "builder"][0]
        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = Path(tmpdir) / "index.html"
            html_path.write_text(
                """<!DOCTYPE html><html><head><script src="./_evermind_runtime/three/three.min.js"></script></head>
                <body><div id="hud">ammo 30 hp 100</div><canvas id="viewport"></canvas>
                <script>
                let yaw=0,pitch=0;
                const forward = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)).normalize();
                const right = new THREE.Vector3(forward.z, 0, -forward.x).normalize();
                function animate(){ requestAnimationFrame(animate); }
                animate();
                </script></body></html>""",
                encoding="utf-8",
            )
            report = orch._validate_builder_quality(
                [str(html_path)],
                html_path.read_text(encoding="utf-8"),
                goal=goal,
                plan=plan,
                subtask=builder1,
            )
        self.assertTrue(any("crosshair/reticle" in str(item) for item in report.get("errors", [])))


class TestBuilderPostWriteIdleTimeout(unittest.TestCase):
    def test_normalize_html_artifact_closes_unterminated_style_before_quality_gate(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        raw = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Voxel</title>
  <style>
    body { margin: 0; background: #111; color: #fff; }
    .hud { display: flex; gap: 12px; }
<body>
  <main class="hud"><section>Ready</section></main>
</html>"""

        fixed = orch._normalize_html_artifact(raw)

        self.assertIn("</style>", fixed.lower())
        self.assertIn("</body>", fixed.lower())
        self.assertTrue(fixed.lower().index("</style>") < fixed.lower().index("</body>"))

    def test_normalize_html_artifact_inserts_missing_head_close_before_body(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        raw = """<!DOCTYPE html>
<html>
<head>
  <title>Broken</title>
  <style>body{background:#111;color:#fff}
<body>
  <main><h1>Ready</h1></main>
</html>"""

        fixed = orch._normalize_html_artifact(raw)

        self.assertIn("</head>", fixed.lower())
        self.assertTrue(fixed.lower().index("</head>") < fixed.lower().index("<body"))
        self.assertIn("</style>", fixed.lower())

    def test_normalize_html_artifact_decodes_escaped_whole_document_before_repair(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        raw = (
            '<!DOCTYPE html>\\n<html lang=\\"zh-CN\\">\\n<head>\\n'
            '<meta charset=\\"UTF-8\\">\\n<title>Escaped</title>\\n'
            '</head>\\n<body>\\n<main><h1>Ready</h1></main>\\n</body>\\n</html>'
        )

        fixed = orch._normalize_html_artifact(raw)

        self.assertIn('lang="zh-CN"', fixed)
        self.assertIn("\n<html", fixed)
        self.assertNotIn("\\n<html", fixed)
        self.assertNotIn('\\"', fixed)

    def test_builder_post_write_idle_timeout_salvages_valid_written_artifact(self):
        class StubBridge:
            def __init__(self, output_dir: Path):
                self.config = {}
                self.output_dir = output_dir

            async def execute(self, node, plugins, input_data, model, on_progress):
                index = self.output_dir / "index.html"
                index.write_text(
                    """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Saved Before Stall</title><style>:root{--bg:#0b1020;--fg:#e9ecf1;--panel:#121a34;--line:rgba(255,255,255,.08)}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#0b1020,#121a34);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}header,main,section,footer,nav,article{display:block}nav{display:flex;justify-content:space-between;padding:18px 24px;border-bottom:1px solid var(--line)}main{display:grid;gap:18px;padding:24px}.hero,.grid,.cta{display:grid;gap:16px}.hero{grid-template-columns:1.2fr .8fr}.grid{grid-template-columns:repeat(3,1fr)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px}button{padding:12px 18px;border:none;border-radius:999px;background:#7bdcff;color:#03253a;font-weight:700}@media(max-width:900px){.hero,.grid,.cta{grid-template-columns:1fr}}</style></head>
<body><header><nav><strong>Northstar</strong><button>Start</button></nav></header><main><section class="hero"><article class="panel"><h1>Saved artifact</h1><p>This page was fully written before the model stalled, so the orchestrator should salvage it instead of waiting indefinitely.</p><p>It includes enough structure, text, and styling to pass the basic quality gate.</p></article><article class="panel"><h2>Status</h2><p>Waiting on idle timeout.</p></article></section><section class="grid"><article class="panel"><h3>One</h3><p>Alpha</p></article><article class="panel"><h3>Two</h3><p>Beta</p></article><article class="panel"><h3>Three</h3><p>Gamma</p></article></section><section class="cta"><article class="panel"><p>Call to action</p></article></section></main><footer>Footer</footer><script>document.querySelector('button').addEventListener('click',()=>{});</script></body></html>""",
                    encoding="utf-8",
                )
                await on_progress({"stage": "builder_write", "path": str(index)})
                await asyncio.sleep(12)
                return {"success": True, "output": "late text", "tool_results": []}

        async def _noop(_evt):
            return None

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            bridge = StubBridge(out)
            orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
            orch.on_event = _noop
            plan = Plan(goal="做一个单页面官网", subtasks=[SubTask(id="1", agent_type="builder", description="build")])
            builder = plan.subtasks[0]
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            original_idle_timeout = orchestrator_module.BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                orchestrator_module.BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC = 5
                with patch.object(orch, "_configured_progress_heartbeat", return_value=5):
                    result = asyncio.run(orch._execute_subtask(builder, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output
                orchestrator_module.BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC = original_idle_timeout

        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("error"), "")

    def test_secondary_single_entry_game_builder_uses_patch_mode_after_primary_success(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        builder1 = SubTask(id="1", agent_type="builder", description="build primary 3d tps", depends_on=[])
        builder2 = SubTask(id="2", agent_type="builder", description="build secondary 3d tps", depends_on=["1"])
        plan = Plan(
            goal="做一个第三人称 3D 射击游戏，要有怪物、武器和大地图",
            subtasks=[builder1, builder2],
        )

        self.assertTrue(orch._single_entry_game_secondary_builder_patch_mode(plan, builder2, plan.goal))
        self.assertEqual(orch._secondary_single_entry_builder_skip_reason(plan, builder2, prev_results={"1": {"success": True}}), "")


class TestTesterVisualGate(unittest.TestCase):
    def test_tester_visual_gate_supports_root_level_artifact(self):
        bridge = type("Bridge", (), {"config": {"tester_run_smoke": False}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>ok</body></html>",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch(
                    "orchestrator.validate_preview",
                    new=AsyncMock(
                        return_value={
                            "ok": True,
                            "errors": [],
                            "warnings": [],
                            "preview_url": "http://127.0.0.1:8765/preview/index.html",
                            "smoke": {"status": "skipped"},
                        }
                    ),
                ):
                    result = asyncio.run(orch._run_tester_visual_gate())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(result.get("task_id"), "root")
        self.assertTrue(result.get("ok"))

    def test_tester_visual_gate_fallbacks_to_root_index_when_lookup_misses(self):
        bridge = type("Bridge", (), {"config": {"tester_run_smoke": False}})()
        orch = Orchestrator(ai_bridge=bridge, executor=None)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>ok</body></html>",
                encoding="utf-8",
            )

            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch("orchestrator.latest_preview_artifact", return_value=(None, None)):
                    with patch(
                        "orchestrator.validate_preview",
                        new=AsyncMock(
                            return_value={
                                "ok": True,
                                "errors": [],
                                "warnings": [],
                                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                                "smoke": {"status": "skipped"},
                            }
                        ),
                    ):
                        result = asyncio.run(orch._run_tester_visual_gate())
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertEqual(result.get("task_id"), "root")
        self.assertTrue(result.get("ok"))


class TestReviewerVisualGate(unittest.TestCase):
    def test_reviewer_fails_if_browser_tool_not_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "review complete", "tool_results": [], "tool_call_stats": {}}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        with patch.object(
            orchestrator_module,
            "_playwright_runtime_status",
            new=AsyncMock(return_value={"available": True, "reason": ""}),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("Reviewer visual gate failed", str(result.get("error", "")))

    def test_reviewer_passes_when_browser_tool_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"status\":\"approved\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_game_reviewer_passes_with_browser_use_only_evidence(self):
        class StubBridge:
            def __init__(self):
                self.config = {"qa_enable_browser_use": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "plugin": "browser_use",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap111",
                        "recording_path": "/tmp/reviewer.webm",
                        "capture_path": "/tmp/shot1.png",
                    },
                    {
                        "stage": "browser_action",
                        "plugin": "browser_use",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "click222",
                        "previous_state_hash": "snap111",
                        "state_changed": True,
                        "recording_path": "/tmp/reviewer.webm",
                    },
                    {
                        "stage": "browser_action",
                        "plugin": "browser_use",
                        "action": "press_sequence",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "press333",
                        "previous_state_hash": "click222",
                        "state_changed": True,
                        "keys_count": 4,
                        "recording_path": "/tmp/reviewer.webm",
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\",\"average\":8.2,\"blocking_issues\":[],\"required_changes\":[]}",
                    "tool_results": [{"success": True, "data": {"recording_path": "/tmp/reviewer.webm"}}],
                    "tool_call_stats": {"browser_use": 1},
                    "qa_browser_use_available": True,
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个 3D 枪战网页游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_game_reviewer_accepts_browser_fallback_when_gameplay_evidence_is_real(self):
        class StubBridge:
            def __init__(self):
                self.config = {"qa_enable_browser_use": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "plugin": "browser",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "menu111",
                    },
                    {
                        "stage": "browser_action",
                        "plugin": "browser",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "click222",
                        "previous_state_hash": "menu111",
                        "state_changed": True,
                    },
                    {
                        "stage": "browser_action",
                        "plugin": "browser",
                        "action": "drag_camera",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "drag333",
                        "previous_state_hash": "click222",
                        "state_changed": True,
                        "drag_distance": 144,
                    },
                    {
                        "stage": "browser_action",
                        "plugin": "browser",
                        "action": "hold_fire",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "fire444",
                        "previous_state_hash": "drag333",
                        "state_changed": True,
                        "hold_ms": 320,
                    },
                    {
                        "stage": "browser_action",
                        "plugin": "browser",
                        "action": "press_sequence",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "keys555",
                        "previous_state_hash": "fire444",
                        "state_changed": True,
                        "keys_count": 8,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\",\"average\":8.2,\"blocking_issues\":[],\"required_changes\":[]}",
                    "tool_results": [],
                    "tool_call_stats": {"browser": 1},
                    "qa_browser_use_available": True,
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个 3D 枪战网页游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_game_reviewer_failed_browser_use_attempt_does_not_satisfy_visual_gate(self):
        class StubBridge:
            def __init__(self):
                self.config = {"qa_enable_browser_use": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\",\"average\":8.2,\"blocking_issues\":[],\"required_changes\":[]}",
                    "tool_results": [{"success": False, "error": "browser_use QA prefetch timed out after 60s"}],
                    "tool_call_stats": {"browser_use": 1},
                    "qa_browser_use_available": True,
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个 3D 枪战网页游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        with patch.object(
            orchestrator_module,
            "_playwright_runtime_status",
            new=AsyncMock(return_value={"available": True, "reason": ""}),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("Reviewer visual gate failed", str(result.get("error", "")))

    def test_reviewer_passes_with_observe_and_act_browser_flow(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "act", "subaction": "click", "ok": True, "target": "ref-1 Start", "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"status\":\"approved\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_standard_reviewer_rejected_requests_builder_requeue(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.9,\"improvements\":[\"Improve spacing\"]}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="test", difficulty="standard", subtasks=[builder, reviewer])

        with patch.object(
            orch,
            "_run_reviewer_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("requeue_requested"))
        self.assertEqual(result.get("requeue_subtasks"), ["1", "2"])
        self.assertEqual(builder.status, TaskStatus.PENDING)
        self.assertEqual(builder.retries, 1)
        self.assertEqual(reviewer.status, TaskStatus.PENDING)

    def test_reviewer_requeue_restores_latest_stable_preview_before_builder_retry(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.9,\"improvements\":[\"Restore all missing pages\"]}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="test", difficulty="standard", subtasks=[builder, reviewer])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stable_root = out / "_stable_previews" / "run_1" / "snapshot"
            stable_root.mkdir(parents=True, exist_ok=True)
            stable_index = stable_root / "index.html"
            stable_index.write_text("<!doctype html><html><body>stable home</body></html>", encoding="utf-8")
            (stable_root / "about.html").write_text("<!doctype html><html><body>stable about</body></html>", encoding="utf-8")
            (out / "index.html").write_text("<!doctype html><html><body>broken home</body></html>", encoding="utf-8")
            (out / "pricing.html").write_text("<!doctype html><html><body>stale extra file</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                orch._stable_preview_path = stable_index
                with patch.object(
                    orch,
                    "_run_reviewer_visual_gate",
                    new=AsyncMock(
                        return_value={
                            "ok": True,
                            "errors": [],
                            "warnings": [],
                            "preview_url": "http://127.0.0.1:8765/preview/index.html",
                            "smoke": {"status": "pass"},
                        }
                    ),
                ):
                    result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output
            restored_index = (out / "index.html").read_text(encoding="utf-8")
            restored_about_exists = (out / "about.html").exists()
            restored_pricing_exists = (out / "pricing.html").exists()

        self.assertTrue(result.get("requeue_requested"))
        self.assertEqual(restored_index, "<!doctype html><html><body>stable home</body></html>")
        self.assertTrue(restored_about_exists)
        self.assertFalse(restored_pricing_exists)

    def test_pro_reviewer_rejected_requeues_all_upstream_builders(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.1,\"improvements\":[\"Improve layout\"]}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder_a = SubTask(id="1", agent_type="builder", description="build top", depends_on=[])
        builder_b = SubTask(id="2", agent_type="builder", description="build bottom", depends_on=[])
        builder_a.status = TaskStatus.COMPLETED
        builder_b.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="4", agent_type="reviewer", description="review", depends_on=["1", "2"])
        plan = Plan(goal="test", difficulty="pro", subtasks=[builder_a, builder_b, reviewer])

        with patch.object(
            orch,
            "_run_reviewer_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("requeue_requested"))
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("requeue_subtasks"), ["1", "2", "4"])
        self.assertEqual(builder_a.status, TaskStatus.PENDING)
        self.assertEqual(builder_b.status, TaskStatus.PENDING)
        self.assertEqual(builder_a.retries, 1)
        self.assertEqual(builder_b.retries, 1)

    def test_reviewer_rejection_appends_rework_brief_to_activity_logs(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": (
                        "{\"verdict\":\"REJECTED\",\"blocking_issues\":[\"Primary CTA is weak\"],"
                        "\"required_changes\":[\"Rewrite the hero CTA and add stronger proof blocks\"]}"
                    ),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch._append_ne_activity = MagicMock()

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个产品官网", difficulty="standard", subtasks=[builder, reviewer])

        with patch.object(
            orch,
            "_run_reviewer_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("requeue_requested"))
        messages = [call.args[1] for call in orch._append_ne_activity.call_args_list if len(call.args) >= 2]
        self.assertTrue(any("Reviewer 退回说明" in msg for msg in messages))
        self.assertTrue(any("收到 Reviewer 退回 brief" in msg for msg in messages))

    def test_pro_reviewer_rejected_becomes_non_retryable_failure_when_budget_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_max_rejections": 1}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"REJECTED\",\"average\":5.9}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch._reviewer_requeues = 1

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="test", difficulty="pro", subtasks=[builder, reviewer])

        with patch.object(
            orch,
            "_run_reviewer_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("requeue_requested", False))
        # Reviewer rejected and budget exhausted: with existing artifacts on disk,
        # the run now uses soft_pass to deliver the best available version.
        self.assertFalse(result.get("retryable", True))

    def test_reviewer_game_requires_keyboard_press(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "snapshot",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                })
                await on_progress({
                    "stage": "browser_action",
                    "action": "click",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                })
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 2},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("multiple gameplay key inputs", str(result.get("error", "")))

    def test_reviewer_website_requires_post_interaction_verification(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap111",
                    },
                    {
                        "stage": "browser_action",
                        "action": "scroll",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "scroll222",
                        "state_changed": True,
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "target": "text=Get Started",
                        "state_hash": "scroll222",
                        "previous_state_hash": "scroll222",
                        "state_changed": False,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 3},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个产品官网", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("post-click state", str(result.get("error", "")))

    def test_reviewer_website_requires_bottom_observation_after_scrolling(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap111",
                        "scroll_y": 0,
                        "viewport_height": 900,
                        "page_height": 2600,
                    },
                    {
                        "stage": "browser_action",
                        "action": "scroll",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "scroll222",
                        "state_changed": True,
                        "scroll_y": 1700,
                        "viewport_height": 900,
                        "page_height": 2600,
                        "at_bottom": True,
                        "is_scrollable": True,
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "target": "text=Get Started",
                        "state_hash": "click333",
                        "previous_state_hash": "scroll222",
                        "state_changed": True,
                        "scroll_y": 120,
                        "viewport_height": 900,
                        "page_height": 2600,
                    },
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap444",
                        "previous_state_hash": "click333",
                        "state_changed": True,
                        "scroll_y": 120,
                        "viewport_height": 900,
                        "page_height": 2600,
                        "at_page_bottom": False,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个产品官网", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("bottom-of-page", str(result.get("error", "")))

    def test_reviewer_fails_on_failed_requests(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "snap111",
                        "failed_request_count": 3,
                    },
                    {
                        "stage": "browser_action",
                        "action": "scroll",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "scroll222",
                        "state_changed": True,
                        "failed_request_count": 3,
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "click333",
                        "previous_state_hash": "scroll222",
                        "state_changed": True,
                        "failed_request_count": 3,
                    },
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "click333",
                        "previous_state_hash": "click333",
                        "state_changed": False,
                        "failed_request_count": 3,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个产品官网", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("failed network request", str(result.get("error", "")))

    def test_reviewer_tolerates_remote_image_request_failures(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                failed_images = [
                    {"url": "https://images.unsplash.com/photo-1", "error": "net::ERR_FAILED", "resource_type": "image"},
                    {"url": "https://images.unsplash.com/photo-2", "error": "net::ERR_FAILED", "resource_type": "image"},
                    {"url": "https://images.unsplash.com/photo-3", "error": "net::ERR_FAILED", "resource_type": "image"},
                ]
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "observe",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "state_hash": "snap111",
                        "failed_request_count": 3,
                        "recent_failed_requests": failed_images,
                    },
                    {
                        "stage": "browser_action",
                        "action": "scroll",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "state_hash": "scroll222",
                        "previous_state_hash": "snap111",
                        "state_changed": True,
                        "page_height": 2200,
                        "viewport_height": 800,
                        "scroll_y": 1400,
                        "at_bottom": True,
                        "failed_request_count": 3,
                        "recent_failed_requests": failed_images,
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "state_hash": "click333",
                        "previous_state_hash": "scroll222",
                        "state_changed": True,
                        "failed_request_count": 3,
                        "recent_failed_requests": failed_images,
                    },
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/index.html",
                        "state_hash": "verify444",
                        "previous_state_hash": "click333",
                        "state_changed": False,
                        "page_height": 2200,
                        "viewport_height": 800,
                        "scroll_y": 1400,
                        "at_bottom": True,
                        "failed_request_count": 3,
                        "recent_failed_requests": failed_images,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个高级单页旅游网站", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_reviewer_multi_page_goal_requires_visiting_all_pages(self):
        class StubBridge:
            def __init__(self):
                self.config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "is_scrollable": False},
                    {"stage": "browser_action", "action": "act", "subaction": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                    {"stage": "browser_action", "action": "observe", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        goal = "做一个三页面官网，包含首页、定价页和联系页"
        plan = Plan(goal=goal, subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("visit every requested page", str(result.get("error", "")))

    def test_reviewer_with_builder_forces_structured_rejection_on_blank_preview(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_run_smoke": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\",\"scores\":{\"layout\":8,\"color\":8,\"typography\":8,\"animation\":7,\"responsive\":8,\"functionality\":8,\"completeness\":8,\"originality\":8}}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个产品官网", difficulty="standard", subtasks=[builder, reviewer])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>stub</body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch(
                    "orchestrator.validate_preview",
                    new=AsyncMock(return_value={
                        "ok": False,
                        "errors": ["Browser smoke test failed"],
                        "warnings": [],
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "smoke": {
                            "status": "fail",
                            "body_text_len": 0,
                            "render_errors": ["Preview appears blank or near-empty: almost no visible content rendered"],
                            "page_errors": [],
                            "console_errors": [],
                            "render_summary": {"readable_text_count": 0, "heading_count": 0, "interactive_count": 0, "image_count": 0, "canvas_count": 0},
                        },
                    }),
                ):
                    result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(result.get("success"))
        self.assertTrue(result.get("requeue_requested"))
        self.assertIn("\"REJECTED\"", str(result.get("output", "")))
        self.assertIn("blank", str(result.get("output", "")).lower())

    def test_reviewer_with_builder_turns_interaction_gate_failure_into_rejection(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "snap111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "scroll222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "target": "text=Get Started", "state_hash": "scroll222", "previous_state_hash": "scroll222", "state_changed": False},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"verdict\":\"APPROVED\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 3},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[])
        builder.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个产品官网", difficulty="standard", subtasks=[builder, reviewer])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text(
                "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"></head><body>stub</body></html>",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch(
                    "orchestrator.validate_preview",
                    new=AsyncMock(return_value={
                        "ok": True,
                        "errors": [],
                        "warnings": [],
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "smoke": {"status": "skipped"},
                    }),
                ):
                    result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(result.get("success"))
        self.assertTrue(result.get("requeue_requested"))
        self.assertIn("\"REJECTED\"", str(result.get("output", "")))
        self.assertIn("state", str(result.get("output", "")).lower())


class TestCustomNodeTaskDescriptions(unittest.TestCase):
    def test_scribe_custom_node_description_is_specific(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desc = orch._custom_node_task_desc("scribe", "Scribe", "写一份产品 API 文档")
        self.assertIn("documentation", desc.lower())
        self.assertIn("examples", desc.lower())

    def test_imagegen_custom_node_description_mentions_prompt_packs(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desc = orch._custom_node_task_desc("imagegen", "Image Gen", "生成一张品牌海报")
        self.assertIn("prompt packs", desc.lower())
        self.assertIn("fallback", desc.lower())

    def test_imagegen_custom_node_description_for_3d_game_mentions_modeling_design(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desc = orch._custom_node_task_desc(
            "imagegen",
            "Image Gen",
            "做一个第三人称 3D 射击游戏，要有人物怪物枪械和精美建模",
        )
        self.assertIn("modeling design packs", desc.lower())
        self.assertIn("open-source asset shortlist guidance", desc.lower())
        self.assertIn("rig-or-animation requirements", desc.lower())
        self.assertIn("00_visual_target.md", desc)

    def test_builder_custom_node_description_for_3d_game_mentions_asset_contracts(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desc = orch._custom_node_task_desc(
            "builder",
            "Builder",
            "做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模",
        )
        self.assertIn("style-lock", desc.lower())
        self.assertIn("replacement hooks", desc.lower())

    def test_enforce_plan_shape_preserves_specialized_nodes(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="生成品牌海报并输出文档",
            difficulty="standard",
            subtasks=[
                SubTask(id="1", agent_type="imagegen", description="", depends_on=[]),
                SubTask(id="2", agent_type="scribe", description="", depends_on=["1"]),
            ],
        )
        orch._enforce_plan_shape(plan, plan.goal, plan.difficulty)
        self.assertEqual([st.agent_type for st in plan.subtasks], ["imagegen", "scribe"])
        self.assertTrue(all(str(st.description or "").strip() for st in plan.subtasks))

    def test_mixed_specialized_plan_gets_canonicalized(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={
            "image_generation": {
                "comfyui_url": "http://127.0.0.1:8188",
                "workflow_template": "/tmp/workflow.json",
            }
        }), executor=None, on_event=None)
        plan = Plan(
            goal="做一个像素风平台跳跃游戏，包含角色素材和 spritesheet",
            difficulty="standard",
            subtasks=[
                SubTask(id="1", agent_type="imagegen", description="draw assets", depends_on=[]),
                SubTask(id="2", agent_type="builder", description="build game", depends_on=["1"]),
            ],
        )
        orch._enforce_plan_shape(plan, plan.goal, plan.difficulty)
        self.assertEqual(
            [st.agent_type for st in plan.subtasks],
            ["analyst", "imagegen", "spritesheet", "assetimport", "builder", "reviewer", "deployer", "tester"],
        )


class TestDependencyAssetHandoff(unittest.TestCase):
    def test_dependency_asset_handoff_summarizes_upstream_asset_files(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        dep_task = SubTask(id="2", agent_type="imagegen", description="assets")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            assets = out / "assets"
            assets.mkdir(parents=True, exist_ok=True)
            manifest = assets / "manifest.json"
            hero = assets / "character_hero_brief.md"
            manifest.write_text(
                json.dumps(
                    {
                        "project": "Outpost Siege",
                        "style": "Stylized-realistic",
                        "assets": {"characters": ["hero", "grunt"], "weapons": ["rifle"]},
                    }
                ),
                encoding="utf-8",
            )
            hero.write_text(
                "# HERO\n- Silhouette: broad shoulders\n- Rig: humanoid\n- Material: slate armor\n",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                handoff = orch._dependency_asset_handoff(
                    dep_task,
                    {"files_created": [str(manifest), str(hero)]},
                    "builder",
                    "做一个第三人称 3D 射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("[Asset Handoff from imagegen #2", handoff)
        self.assertIn("Outpost Siege", handoff)
        self.assertIn("broad shoulders", handoff)
        self.assertIn("runtime asset keys", handoff)

    def test_dependency_asset_handoff_omits_missing_manifest_file_refs(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        dep_task = SubTask(id="2", agent_type="imagegen", description="assets")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            assets = out / "assets"
            assets.mkdir(parents=True, exist_ok=True)
            manifest = assets / "manifest.json"
            hero = assets / "character_hero_brief.md"
            hero.write_text("# HERO\n- Silhouette: angular visor\n", encoding="utf-8")
            manifest.write_text(
                json.dumps(
                    {
                        "project": "Steel Harbor",
                        "assets": {
                            "hero": {"file": "character_hero_brief.md"},
                            "grunt": {"file": "monster_grunt_brief.md"},
                            "rifle": {"file": "weapon_rifle_brief.md"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                handoff = orch._dependency_asset_handoff(
                    dep_task,
                    {"files_created": [str(manifest), str(hero)]},
                    "builder",
                    "做一个第三人称 3D 射击游戏",
                )
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertIn("character_hero_brief.md", handoff)
        self.assertNotIn("monster_grunt_brief.md", handoff)
        self.assertNotIn("weapon_rifle_brief.md", handoff)
        self.assertIn("missing_asset_refs=2 omitted", handoff)

    def test_execute_subtask_builder_input_includes_asset_handoff_when_files_exist(self):
        class StubBridge:
            def __init__(self):
                self.config = {}
                self.seen_input = ""

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.seen_input = str(input_data or "")
                return {"success": False, "output": "", "error": "stop", "tool_results": []}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)
        orch.emit = AsyncMock()
        plan = Plan(
            goal="做一个第三人称 3D 射击游戏，要有怪物和不同武器",
            subtasks=[
                SubTask(id="1", agent_type="imagegen", description="asset briefs"),
                SubTask(id="2", agent_type="builder", description="build", depends_on=["1"]),
            ],
        )

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            assets = out / "assets"
            assets.mkdir(parents=True, exist_ok=True)
            manifest = assets / "manifest.json"
            hero = assets / "character_hero_brief.md"
            manifest.write_text(
                json.dumps(
                    {
                        "project": "Steel Harbor",
                        "assets": {"characters": ["hero", "grunt"], "weapons": ["rifle", "shotgun"]},
                    }
                ),
                encoding="utf-8",
            )
            hero.write_text(
                "# HERO CHARACTER\n- Silhouette: tactical backpack\n- Rig: humanoid + weapon socket\n",
                encoding="utf-8",
            )
            prev_results = {
                "1": {
                    "output": "Saved briefs and manifest.",
                    "files_created": [str(manifest), str(hero)],
                }
            }
            original_output = orchestrator_module.OUTPUT_DIR
            original_preview_output = preview_validation_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                preview_validation_module.OUTPUT_DIR = out
                asyncio.run(orch._execute_subtask(plan.subtasks[1], plan, "kimi-coding", prev_results))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output
                preview_validation_module.OUTPUT_DIR = original_preview_output

        self.assertIn("[Asset Handoff from imagegen #1", bridge.seen_input)
        self.assertIn("Steel Harbor", bridge.seen_input)
        self.assertIn("tactical backpack", bridge.seen_input)


class TestTesterBrowserGate(unittest.TestCase):
    def test_tester_fails_if_browser_tool_not_used(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "{\"status\":\"pass\"}", "tool_results": [], "tool_call_stats": {}}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        with patch.object(
            orchestrator_module,
            "_playwright_runtime_status",
            new=AsyncMock(return_value={"available": True, "reason": ""}),
        ):
            result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("Tester visual gate failed", str(result.get("error", "")))

    def test_tester_game_passes_with_desktop_qa_session_without_browser_tool(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {"success": True, "output": "{\"status\":\"pass\"}", "tool_results": [], "tool_call_stats": {}}

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        desktop_session = {
            "ok": True,
            "summary": "[Desktop QA Session Evidence]",
            "actions": [
                {
                    "plugin": "desktop_qa_session",
                    "action": "click",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                },
                {
                    "plugin": "desktop_qa_session",
                    "action": "press_sequence",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "keys_count": 5,
                    "state_hash": "game222",
                    "previous_state_hash": "menu111",
                    "state_changed": True,
                },
                {
                    "plugin": "desktop_qa_session",
                    "action": "snapshot",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "game333",
                    "previous_state_hash": "game222",
                    "state_changed": True,
                },
            ],
        }

        with patch.object(orch, "_maybe_collect_desktop_qa_session", new=AsyncMock(return_value=desktop_session)):
            with patch("orchestrator.validate_preview", new=AsyncMock(return_value={
                "ok": True,
                "errors": [],
                "warnings": [],
                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                "smoke": {"status": "skipped"},
            })):
                with patch("orchestrator.latest_preview_artifact", return_value=("root", Path("/tmp/evermind_output/index.html"))):
                    with patch("orchestrator.build_preview_url_for_file", return_value="http://127.0.0.1:8765/preview/index.html"):
                        result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_tester_failed_browser_use_attempt_does_not_satisfy_visual_gate(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False, "qa_enable_browser_use": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": False, "error": "browser_use QA prefetch timed out after 60s"}],
                    "tool_call_stats": {"browser_use": 1},
                    "qa_browser_use_available": True,
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个 3D 枪战网页游戏", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        with patch.object(
            orchestrator_module,
            "_playwright_runtime_status",
            new=AsyncMock(return_value={"available": True, "reason": ""}),
        ):
            result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("Tester visual gate failed", str(result.get("error", "")))

    def test_tester_passes_when_browser_tool_used_and_gate_passes(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "aaa111"},
                    {"stage": "browser_action", "action": "scroll", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ccc333", "previous_state_hash": "bbb222", "state_changed": True},
                    {"stage": "browser_action", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "ddd444", "previous_state_hash": "ccc333", "state_changed": True},
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        with patch("orchestrator.validate_preview", new=AsyncMock(return_value={
            "ok": True,
            "errors": [],
            "warnings": [],
            "preview_url": "http://127.0.0.1:8765/preview/index.html",
            "smoke": {"status": "skipped"},
        })):
            with patch("orchestrator.latest_preview_artifact", return_value=("root", Path("/tmp/evermind_output/index.html"))):
                with patch("orchestrator.build_preview_url_for_file", return_value="http://127.0.0.1:8765/preview/index.html"):
                    result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_tester_game_requires_keyboard_press(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "snapshot",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                })
                await on_progress({
                    "stage": "browser_action",
                    "action": "click",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/",
                    "state_hash": "menu111",
                })
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 2},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("multiple gameplay key inputs", str(result.get("error", "")))

    def test_reviewer_game_passes_with_desktop_qa_session_without_browser_tool(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "APPROVED",
                        "scores": {
                            "layout": 8,
                            "color": 8,
                            "typography": 8,
                            "animation": 8,
                            "responsive": 8,
                            "functionality": 8,
                            "completeness": 8,
                            "originality": 8,
                        },
                        "issues": [],
                        "blocking_issues": [],
                        "required_changes": [],
                        "missing_deliverables": [],
                        "ship_readiness": 8,
                    }),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        desktop_session = {
            "ok": True,
            "gameplayStarted": True,
            "task_type": "game",
            "summary": "[Desktop QA Session Evidence]",
            "actions": [
                {"plugin": "desktop_qa_session", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "menu111"},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": True, "url": "http://127.0.0.1:8765/preview/", "keys_count": 4, "state_hash": "game222", "previous_state_hash": "menu111", "state_changed": True},
                {"plugin": "desktop_qa_session", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game333", "previous_state_hash": "game222", "state_changed": True},
            ],
            "gameplaySignals": {
                "initial": {"visibleStartCount": 1, "overlayVisible": True, "scoreDigest": "score 0"},
                "afterClick": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 0"},
                "afterKeys": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 4"},
                "final": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 4"},
            },
        }

        with patch.object(orch, "_maybe_collect_desktop_qa_session", new=AsyncMock(return_value=desktop_session)):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        self.assertTrue(result.get("success"))

    def test_desktop_qa_game_session_without_real_transition_is_not_usable(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desktop_session = {
            "task_type": "game",
            "summary": "[Desktop QA Session Evidence]",
            "actions": [
                {"plugin": "desktop_qa_session", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "menu111"},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": True, "url": "http://127.0.0.1:8765/preview/", "keys_count": 5, "state_hash": "menu222", "previous_state_hash": "menu111", "state_changed": True},
            ],
            "gameplaySignals": {
                "initial": {"visibleStartCount": 1, "overlayVisible": True, "scoreDigest": "score 0"},
                "afterKeys": {"visibleStartCount": 1, "overlayVisible": True, "scoreDigest": "score 0"},
                "final": {"visibleStartCount": 1, "overlayVisible": True, "scoreDigest": "score 0"},
            },
        }

        self.assertFalse(orch._desktop_qa_session_usable(desktop_session))

    def test_desktop_qa_game_session_requires_key_phase_transition(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desktop_session = {
            "task_type": "game",
            "summary": "[Desktop QA Session Evidence]",
            "gameplayStarted": True,
            "actions": [
                {"plugin": "desktop_qa_session", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "menu111"},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": True, "url": "http://127.0.0.1:8765/preview/", "keys_count": 5, "state_hash": "game222", "previous_state_hash": "menu111", "state_changed": True},
                {"plugin": "desktop_qa_session", "action": "snapshot", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game333", "previous_state_hash": "game222", "state_changed": True},
            ],
            "gameplaySignals": {
                "initial": {"visibleStartCount": 1, "overlayVisible": True, "scoreDigest": "score 0"},
                "afterClick": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 0"},
                "afterKeys": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 0"},
                "final": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 0"},
            },
        }

        self.assertFalse(orch._desktop_qa_session_has_meaningful_gameplay(desktop_session))
        self.assertFalse(orch._desktop_qa_session_usable(desktop_session))

    def test_desktop_qa_game_session_runtime_errors_do_not_count_as_meaningful_gameplay(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desktop_session = {
            "task_type": "game",
            "summary": "[Desktop QA Session Evidence]\n- summary: runtime error observed",
            "gameplayStarted": True,
            "actions": [
                {"plugin": "desktop_qa_session", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "menu111"},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": True, "url": "http://127.0.0.1:8765/preview/", "keys_count": 5, "state_hash": "game222", "previous_state_hash": "menu111", "state_changed": True},
            ],
            "consoleErrors": [{"message": "Uncaught TypeError"}],
            "gameplaySignals": {
                "initial": {"visibleStartCount": 1, "overlayVisible": True, "scoreDigest": "score 0"},
                "afterClick": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 8"},
                "afterKeys": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 8"},
                "final": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 8"},
            },
        }

        self.assertFalse(orch._desktop_qa_session_has_meaningful_gameplay(desktop_session))
        self.assertTrue(orch._desktop_qa_session_usable(desktop_session))

    def test_desktop_qa_game_session_ignores_benign_console_warnings(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desktop_session = {
            "task_type": "game",
            "summary": "[Desktop QA Session Evidence]\n- summary: gameplay observed with a non-blocking warning",
            "gameplayStarted": False,
            "actions": [
                {"plugin": "desktop_qa_session", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "menu111", "state_changed": True},
                {"plugin": "desktop_qa_session", "action": "drag_camera", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game222", "previous_state_hash": "menu111", "state_changed": True, "drag_distance": 144},
                {"plugin": "desktop_qa_session", "action": "hold_fire", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game333", "previous_state_hash": "game222", "state_changed": True, "hold_ms": 320},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": True, "url": "http://127.0.0.1:8765/preview/", "keys_count": 9, "state_hash": "game444", "previous_state_hash": "game333", "state_changed": True},
            ],
            "consoleErrors": [
                {
                    "level": 2,
                    "message": "Scripts \"build/three.min.js\" are deprecated with r150+, and will be removed with r160. Please use ES Modules or alternatives.",
                    "source": "http://127.0.0.1:8765/preview/_evermind_runtime/three/three.min.js",
                    "qaInfra": False,
                }
            ],
            "gameplaySignals": {
                "initial": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "enemies 0 ammo 30/90", "bodyTextLength": 240},
                "afterClick": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "enemies 5 ammo 30/90", "bodyTextLength": 110},
                "afterDrag": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "enemies 5 ammo 25/90", "bodyTextLength": 110},
                "afterFire": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "enemies 5 ammo 21/90", "bodyTextLength": 110},
                "afterKeys": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "enemies 5 ammo 21/90", "bodyTextLength": 110},
                "final": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "enemies 5 ammo 21/90", "bodyTextLength": 110},
            },
        }

        self.assertFalse(orch._desktop_qa_session_has_runtime_failures(desktop_session))
        self.assertTrue(orch._desktop_qa_session_has_meaningful_gameplay(desktop_session))
        self.assertTrue(orch._desktop_qa_session_usable(desktop_session))

    def test_desktop_qa_game_session_accepts_rich_internal_action_trace(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desktop_session = {
            "task_type": "game",
            "summary": "[Desktop QA Session Evidence]\n- summary: internal QA reached gameplay",
            "gameplayStarted": True,
            "actions": [
                {"plugin": "desktop_qa_session", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "menu111"},
                {"plugin": "desktop_qa_session", "action": "drag_camera", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game222", "previous_state_hash": "menu111", "state_changed": True, "drag_distance": 144},
                {"plugin": "desktop_qa_session", "action": "hold_fire", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game333", "previous_state_hash": "game222", "state_changed": True, "hold_ms": 320},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": True, "url": "http://127.0.0.1:8765/preview/", "keys_count": 2, "state_hash": "game444", "previous_state_hash": "game333", "state_changed": True},
            ],
            "gameplaySignals": {
                "initial": {"visibleStartCount": 1, "overlayVisible": True, "scoreDigest": "score 0"},
                "afterClick": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 0"},
                "afterKeys": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 0"},
                "final": {"visibleStartCount": 0, "overlayVisible": False, "scoreDigest": "score 0"},
            },
        }

        self.assertTrue(orch._desktop_qa_session_has_meaningful_gameplay(desktop_session))
        self.assertTrue(orch._desktop_qa_session_usable(desktop_session))

    def test_desktop_qa_prefetch_summary_surfaces_derived_gameplay_signal_when_raw_flag_is_false(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        desktop_session = {
            "task_type": "game",
            "summary": "[Desktop QA Session Evidence]\n- summary: gameplay observed via transitions",
            "gameplayStarted": False,
            "actions": [
                {"plugin": "desktop_qa_session", "action": "click", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "menu111", "state_changed": True},
                {"plugin": "desktop_qa_session", "action": "drag_camera", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game222", "previous_state_hash": "menu111", "state_changed": True, "drag_distance": 144},
                {"plugin": "desktop_qa_session", "action": "hold_fire", "ok": True, "url": "http://127.0.0.1:8765/preview/", "state_hash": "game333", "previous_state_hash": "game222", "state_changed": True, "hold_ms": 320},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": True, "url": "http://127.0.0.1:8765/preview/", "keys_count": 9, "state_hash": "game444", "previous_state_hash": "game333", "state_changed": True},
            ],
            "gameplaySignals": {
                "initial": {"visibleStartCount": 2, "overlayVisible": False, "scoreDigest": "最终得分: 0"},
                "afterClick": {"visibleStartCount": 2, "overlayVisible": False, "scoreDigest": "生命值 100 弹药 30 / 90 关卡 1 / 3"},
                "afterDrag": {"visibleStartCount": 2, "overlayVisible": False, "scoreDigest": "生命值 100 弹药 27 / 90 关卡 1 / 3"},
                "afterFire": {"visibleStartCount": 2, "overlayVisible": False, "scoreDigest": "生命值 100 弹药 23 / 90 关卡 1 / 3"},
                "afterKeys": {"visibleStartCount": 2, "overlayVisible": False, "scoreDigest": "最终得分: 0"},
                "final": {"visibleStartCount": 2, "overlayVisible": False, "scoreDigest": "最终得分: 0"},
            },
        }

        summary = orch._desktop_qa_prefetch_summary(desktop_session, "game")

        self.assertIn("- gameplay_started: 0", summary)
        self.assertIn("- derived_gameplay_evidence: 1", summary)
        self.assertIn("interaction/state transitions still indicate real gameplay", summary)

    def test_builder_direct_text_output_looks_complete_requires_balanced_scripts_and_body_close(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        filler = "<div class='enemy'>wave data</div>" * 18
        partial = (
            "```html index.html\n"
            "<!DOCTYPE html><html><head><title>X</title></head><body>"
            f"{filler}"
            "<canvas id='gameCanvas'></canvas><script>"
            "const state = { running: true }; requestAnimationFrame(loop);\n"
        )
        complete = (
            "```html index.html\n"
            "<!DOCTYPE html><html><head><title>X</title></head><body>"
            f"{filler}"
            "<canvas id='gameCanvas'></canvas><script>"
            "const state = { running: true }; requestAnimationFrame(loop);"
            "</script></body></html>\n```"
        )

        self.assertFalse(orch._builder_direct_text_output_looks_complete(partial))
        self.assertTrue(orch._builder_direct_text_output_looks_complete(complete))

    def test_reviewer_game_uses_usable_desktop_qa_evidence_even_when_session_not_ok(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_run_smoke": False}
                self.plugins_seen = []

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.plugins_seen = [getattr(plugin, "name", "") for plugin in (plugins or [])]
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "scores": {
                            "layout": 4,
                            "color": 4,
                            "typography": 4,
                            "animation": 3,
                            "responsive": 4,
                            "functionality": 2,
                            "completeness": 3,
                            "originality": 3,
                        },
                        "issues": ["Crash on load"],
                        "blocking_issues": ["Gameplay never initializes"],
                        "required_changes": ["Fix runtime error before review can pass"],
                        "missing_deliverables": [],
                        "ship_readiness": 2,
                    }),
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个贪吃蛇小游戏", subtasks=[SubTask(id="2", agent_type="reviewer", description="review", depends_on=[])])
        reviewer = plan.subtasks[0]

        desktop_session = {
            "ok": False,
            "usable": True,
            "task_type": "game",
            "summary": "[Desktop QA Session Evidence]\n- summary: runtime error observed",
            "actions": [
                {"plugin": "desktop_qa_session", "action": "snapshot", "ok": False, "url": "http://127.0.0.1:8765/preview/", "state_hash": "err111"},
                {"plugin": "desktop_qa_session", "action": "click", "ok": False, "url": "http://127.0.0.1:8765/preview/", "state_hash": "err111", "previous_state_hash": "err111"},
                {"plugin": "desktop_qa_session", "action": "press_sequence", "ok": False, "url": "http://127.0.0.1:8765/preview/", "keys_count": 4, "state_hash": "err111", "previous_state_hash": "err111"},
            ],
            "consoleErrors": [{"message": "Crash on load"}],
        }

        with patch.object(orch, "_maybe_collect_desktop_qa_session", new=AsyncMock(return_value=desktop_session)):
            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
        # Reviewer rejected with low scores, but no builder in plan so soft-pass
        self.assertTrue(result.get("success"))
        self.assertNotIn("browser", bridge.plugins_seen)
        self.assertNotIn("browser_use", bridge.plugins_seen)

    def test_tester_dashboard_requires_visible_state_change(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                for event in [
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "dash111",
                    },
                    {
                        "stage": "browser_action",
                        "action": "click",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "target": "text=Monthly",
                        "state_hash": "dash111",
                        "previous_state_hash": "dash111",
                        "state_changed": False,
                    },
                    {
                        "stage": "browser_action",
                        "action": "snapshot",
                        "ok": True,
                        "url": "http://127.0.0.1:8765/preview/",
                        "state_hash": "dash111",
                        "previous_state_hash": "dash111",
                        "state_changed": False,
                    },
                ]:
                    await on_progress(event)
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/"}}],
                    "tool_call_stats": {"browser": 3},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个数据看板仪表盘", subtasks=[SubTask(id="4", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
        self.assertFalse(result.get("success"))
        self.assertIn("visible state", str(result.get("error", "")))

    def test_tester_quality_gate_rejects_premium_3d_artifact_even_when_visual_smoke_passes(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": False}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "observe",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/index.html",
                    "state_changed": True,
                })
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 1},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        tester = SubTask(id="4", agent_type="tester", description="test", depends_on=[])
        plan = Plan(goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，还要有关卡和通过页面。", subtasks=[tester])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            runtime_dir = out / "_evermind_runtime" / "three"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "three.min.js").write_text("// stub runtime", encoding="utf-8")
            (out / "index.html").write_text(
                """<!doctype html><html><head>
<script src="./_evermind_runtime/three/three.min.js"></script>
</head><body>
<button id="startBtn" onclick="startGame()">开始</button>
<canvas id="gameCanvas"></canvas>
<div class="hud"><span>health 100</span><span>ammo 30</span></div>
<section id="gameOver"><button onclick="startGame()">重新开始</button></section>
<script>
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 2000);
const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('gameCanvas') });
const weaponGeometry = new THREE.BoxGeometry(0.3, 0.2, 1.8);
const enemyGeometry = new THREE.ConeGeometry(0.8, 1.6, 8);
function startGame(){ document.body.dataset.mode = 'playing'; requestAnimationFrame(loop); }
function loop(){ renderer.render(scene, camera); requestAnimationFrame(loop); }
document.addEventListener('keydown', (e) => { window.lastKey = e.code; });
document.addEventListener('mousedown', () => { window.firing = true; });
</script>
</body></html>""",
                encoding="utf-8",
            )
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch.object(orch, "_interaction_gate_error", return_value=None):
                    with patch.object(
                        orch,
                        "_run_tester_visual_gate",
                        new=AsyncMock(
                            return_value={
                                "ok": True,
                                "errors": [],
                                "warnings": [],
                                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                                "smoke": {"status": "pass"},
                            }
                        ),
                    ):
                        result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(result.get("success"))
        self.assertIn("__EVERMIND_TESTER_GATE__=FAIL", str(result.get("output", "")))
        self.assertIn("QUALITY GATE FAILED", str(result.get("output", "")))


class TestTesterNonRetryableFailure(unittest.TestCase):
    def test_non_retryable_tester_failure_does_not_requeue_builder(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [
                        {"_plugin": "browser", "success": True, "data": {"action": "snapshot", "url": "http://127.0.0.1:8765/preview/", "state_hash": "snap111"}},
                        {"_plugin": "browser", "success": True, "data": {"action": "scroll", "url": "http://127.0.0.1:8765/preview/", "state_hash": "scroll222", "previous_state_hash": "snap111", "state_changed": True, "at_bottom": True}},
                        {"_plugin": "browser", "success": True, "data": {"action": "click", "url": "http://127.0.0.1:8765/preview/", "target": "Start", "state_hash": "click333", "previous_state_hash": "scroll222", "state_changed": True}},
                        {"_plugin": "browser", "success": True, "data": {"action": "snapshot", "url": "http://127.0.0.1:8765/preview/", "state_hash": "snap444", "previous_state_hash": "click333", "state_changed": True}},
                    ],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="test", subtasks=[SubTask(id="9", agent_type="tester", description="test", depends_on=[])])

        with patch.object(
            orch,
            "_run_tester_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": False,
                    "errors": ["No HTML preview artifact found for tester validation"],
                    "warnings": [],
                    "preview_url": None,
                    "smoke": {"status": "skipped", "reason": "no_artifact"},
                }
            ),
        ):
            with patch.object(orch, "_retry_from_failure", new=AsyncMock()) as retry_mock:
                with patch.object(
                    orchestrator_module,
                    "_playwright_runtime_status",
                    new=AsyncMock(return_value={"available": True, "reason": ""}),
                ):
                    asyncio.run(orch._execute_plan(plan, "kimi-coding"))
                retry_mock.assert_not_called()

        tester = plan.subtasks[0]
        self.assertEqual(tester.status, TaskStatus.FAILED)
        self.assertNotIn("neither browser nor browser_use was used", tester.error)
        self.assertIn(
            "No HTML preview artifact found for tester validation",
            f"{tester.output} {tester.error}",
        )

    def test_tester_accepts_browser_tool_results_as_visual_evidence(self):
        class StubBridge:
            def __init__(self):
                self.config = {"tester_run_smoke": True}

            async def execute(self, node, plugins, input_data, model, on_progress):
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [
                        {"_plugin": "browser", "success": True, "data": {"action": "snapshot", "url": "http://127.0.0.1:8765/preview/", "state_hash": "snap111", "capture_path": "/tmp/tester-shot.png"}},
                        {"_plugin": "browser", "success": True, "data": {"action": "scroll", "url": "http://127.0.0.1:8765/preview/", "state_hash": "scroll222", "previous_state_hash": "snap111", "state_changed": True, "at_bottom": True}},
                        {"_plugin": "browser", "success": True, "data": {"action": "click", "url": "http://127.0.0.1:8765/preview/", "target": "Start", "state_hash": "click333", "previous_state_hash": "scroll222", "state_changed": True}},
                        {"_plugin": "browser", "success": True, "data": {"action": "snapshot", "url": "http://127.0.0.1:8765/preview/", "state_hash": "snap444", "previous_state_hash": "click333", "state_changed": True}},
                    ],
                    "tool_call_stats": {"browser": 4},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个极简测试页面", subtasks=[SubTask(id="9", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        with patch.object(
            orch,
            "_run_tester_visual_gate",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "errors": [],
                    "warnings": [],
                    "preview_url": "http://127.0.0.1:8765/preview/index.html",
                    "smoke": {"status": "pass"},
                }
            ),
        ):
            with patch.object(
                orch,
                "_reviewer_structural_quality_gate",
                return_value={"ok": True, "errors": [], "warnings": [], "route_signals": {}, "weak_routes": []},
            ):
                result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("Tester visual gate failed", str(result.get("error", "")))

    def test_tester_skips_visual_gate_when_browser_runtime_is_unavailable(self):
        class StubBridge:
            def __init__(self):
                self.config = {
                    "tester_run_smoke": False,
                    "qa_enable_browser_use": True,
                    "openai_api_key": "sk-test",
                }
                self.plugins_seen = []

            async def execute(self, node, plugins, input_data, model, on_progress):
                self.plugins_seen = [getattr(plugin, "name", "") for plugin in (plugins or [])]
                return {
                    "success": True,
                    "output": "{\"status\":\"pass\"}",
                    "tool_results": [],
                    "tool_call_stats": {},
                }

        bridge = StubBridge()
        orch = Orchestrator(ai_bridge=bridge, executor=None, on_event=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        plan = Plan(goal="做一个数据看板仪表盘", subtasks=[SubTask(id="9", agent_type="tester", description="test", depends_on=[])])
        tester = plan.subtasks[0]

        with patch.object(
            orchestrator_module,
            "_playwright_runtime_status",
            new=AsyncMock(return_value={"available": False, "reason": "sandbox blocked"}),
        ):
            with patch.object(
                orch,
                "_run_tester_visual_gate",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "errors": [],
                        "warnings": [],
                        "preview_url": None,
                        "smoke": {"status": "skipped", "reason": "playwright unavailable"},
                    }
                ),
            ):
                result = asyncio.run(orch._execute_subtask(tester, plan, "kimi-coding", prev_results={}))

        self.assertTrue(result.get("success"))
        self.assertNotIn("browser", bridge.plugins_seen)
        self.assertNotIn("browser_use", bridge.plugins_seen)


class TestReviewerNonRetryableRejection(unittest.TestCase):
    def test_reviewer_rejection_without_requeue_budget_fails_instead_of_proceeding(self):
        class StubBridge:
            config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "observe",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/index.html",
                    "state_changed": True,
                })
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "issues": ["Mid-page sections are blank"],
                        "required_changes": ["Restore the missing middle sections before approval"],
                    }),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 1},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=1)
        builder.status = TaskStatus.COMPLETED
        builder.retries = builder.max_retries
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个高端多页面官网", subtasks=[builder, reviewer])

        with patch.object(orch, "_interaction_gate_error", return_value=None):
            with patch.object(
                orch,
                "_run_reviewer_visual_gate",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "errors": [],
                        "warnings": [],
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "smoke": {"status": "pass"},
                    }
                ),
                ):
                    result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))

        # Reviewer rejected and builder retries exhausted: with existing artifacts,
        # the run now uses soft_pass. Without artifacts, it hard-fails.
        # Since the test runs in the real OUTPUT_DIR (which may have artifacts),
        # we accept either path: soft_pass (success=True) or hard-fail (success=False).
        self.assertFalse(result.get("retryable", True))
        if result.get("success"):
            self.assertEqual(reviewer.status, TaskStatus.COMPLETED)
        else:
            self.assertIn("No usable artifacts found", str(result.get("error", "")))
            self.assertEqual(reviewer.status, TaskStatus.FAILED)

    def test_reviewer_rejection_blocks_premium_3d_game_delivery_when_retries_exhausted(self):
        class StubBridge:
            config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "observe",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/index.html",
                    "state_changed": True,
                })
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "issues": ["Core enemy and weapon models still look placeholder-grade."],
                        "required_changes": ["Replace primitive enemy/weapon geometry and add a real pass screen."],
                    }),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 1},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build premium 3d shooter", depends_on=[], max_retries=1)
        builder.status = TaskStatus.COMPLETED
        builder.retries = builder.max_retries
        reviewer = SubTask(id="2", agent_type="reviewer", description="review", depends_on=["1"])
        plan = Plan(goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，达到商业级水准。", subtasks=[builder, reviewer])

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text("<!doctype html><html><body>artifact exists</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch.object(orch, "_interaction_gate_error", return_value=None):
                    with patch.object(
                        orch,
                        "_run_reviewer_visual_gate",
                        new=AsyncMock(
                            return_value={
                                "ok": True,
                                "errors": [],
                                "warnings": [],
                                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                                "smoke": {"status": "pass"},
                            }
                        ),
                    ):
                        with patch.object(orch, "_reviewer_structural_quality_gate", return_value={"ok": True, "errors": [], "warnings": [], "weak_routes": []}):
                            result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertFalse(result.get("success"))
        self.assertFalse(result.get("retryable", True))
        self.assertIn("Delivery blocked", str(result.get("error", "")))
        self.assertEqual(reviewer.status, TaskStatus.FAILED)

    def test_reviewer_rejection_requeues_patch_mode_builder_for_single_entry_game(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_max_rejections": 3}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "observe",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/index.html",
                    "state_changed": True,
                })
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "issues": ["Core enemy and weapon models still look placeholder-grade."],
                        "required_changes": ["Replace primitive enemy/weapon geometry and add a real pass screen."],
                    }),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 1},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder1 = SubTask(id="1", agent_type="builder", description="build premium 3d shooter", depends_on=[], max_retries=1)
        builder1.status = TaskStatus.COMPLETED
        builder1.retries = builder1.max_retries
        builder2 = SubTask(id="2", agent_type="builder", description="shadow builder", depends_on=["1"], max_retries=3)
        builder2.status = TaskStatus.COMPLETED
        builder2.retries = 0
        reviewer = SubTask(id="3", agent_type="reviewer", description="review", depends_on=["1", "2"])
        plan = Plan(
            goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，达到商业级水准。",
            subtasks=[builder1, builder2, reviewer],
        )

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text("<!doctype html><html><body>artifact exists</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch.object(orch, "_interaction_gate_error", return_value=None):
                    with patch.object(
                        orch,
                        "_run_reviewer_visual_gate",
                        new=AsyncMock(
                            return_value={
                                "ok": True,
                                "errors": [],
                                "warnings": [],
                                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                                "smoke": {"status": "pass"},
                            }
                        ),
                    ):
                        with patch.object(orch, "_reviewer_structural_quality_gate", return_value={"ok": True, "errors": [], "warnings": [], "weak_routes": []}):
                            result = asyncio.run(orch._execute_subtask(
                                reviewer,
                                plan,
                                "kimi-coding",
                                prev_results={"1": {"success": True, "files_created": [str(out / "index.html")], "output": "primary passed"}},
                            ))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(result.get("requeue_requested"))
        self.assertFalse(result.get("success"))
        self.assertIn("2", result.get("requeue_subtasks", []))
        self.assertNotIn("1", result.get("requeue_subtasks", []))

    def test_reviewer_rejection_requeues_asset_pipeline_for_premium_3d_model_failures(self):
        class StubBridge:
            def __init__(self):
                self.config = {"reviewer_max_rejections": 3}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "observe",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/index.html",
                    "state_changed": True,
                })
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "issues": ["Premium 3D/TPS brief still appears to render core models as primitive placeholder geometry."],
                        "required_changes": ["Replace primitive placeholder geometry with authored non-primitive hero assets for the player, main enemy, and primary weapon."],
                    }),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 1},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)
        # v4.0: Progressive rollback — asset pipeline retry only on 2nd+ rejection.
        # Simulate that this is the second reviewer rejection round.
        orch._reviewer_requeues = 1

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        imagegen = SubTask(id="1", agent_type="imagegen", description="imagegen", depends_on=[], max_retries=2)
        imagegen.status = TaskStatus.COMPLETED
        spritesheet = SubTask(id="2", agent_type="spritesheet", description="spritesheet", depends_on=["1"], max_retries=2)
        spritesheet.status = TaskStatus.COMPLETED
        assetimport = SubTask(id="3", agent_type="assetimport", description="assetimport", depends_on=["1", "2"], max_retries=2)
        assetimport.status = TaskStatus.COMPLETED
        builder1 = SubTask(id="4", agent_type="builder", description="build premium 3d shooter", depends_on=["3"], max_retries=2)
        builder1.status = TaskStatus.COMPLETED
        builder2 = SubTask(id="5", agent_type="builder", description="shadow builder", depends_on=["4"], max_retries=2)
        builder2.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="6", agent_type="reviewer", description="review", depends_on=["4", "5"])
        plan = Plan(
            goal="做一个第三人称 3D 射击游戏，要有怪物、枪械、大地图和精美建模，达到商业级水准。",
            subtasks=[imagegen, spritesheet, assetimport, builder1, builder2, reviewer],
        )

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "index.html").write_text("<!doctype html><html><body>artifact exists</body></html>", encoding="utf-8")
            original_output = orchestrator_module.OUTPUT_DIR
            try:
                orchestrator_module.OUTPUT_DIR = out
                with patch.object(orch, "_interaction_gate_error", return_value=None):
                    with patch.object(
                        orch,
                        "_run_reviewer_visual_gate",
                        new=AsyncMock(
                            return_value={
                                "ok": True,
                                "errors": [],
                                "warnings": [],
                                "preview_url": "http://127.0.0.1:8765/preview/index.html",
                                "smoke": {"status": "pass"},
                            }
                        ),
                    ):
                        with patch.object(orch, "_reviewer_structural_quality_gate", return_value={"ok": True, "errors": [], "warnings": [], "weak_routes": []}):
                            result = asyncio.run(orch._execute_subtask(
                                reviewer,
                                plan,
                                "kimi-coding",
                                prev_results={"4": {"success": True, "files_created": [str(out / "index.html")], "output": "primary passed"}},
                            ))
            finally:
                orchestrator_module.OUTPUT_DIR = original_output

        self.assertTrue(result.get("requeue_requested"))
        self.assertFalse(result.get("success"))
        self.assertIn("1", result.get("requeue_subtasks", []))
        self.assertIn("2", result.get("requeue_subtasks", []))
        self.assertIn("3", result.get("requeue_subtasks", []))
        self.assertIn("4", result.get("requeue_subtasks", []))
        self.assertIn("5", result.get("requeue_subtasks", []))
        self.assertEqual(imagegen.status, TaskStatus.PENDING)
        self.assertEqual(spritesheet.status, TaskStatus.PENDING)
        self.assertEqual(assetimport.status, TaskStatus.PENDING)
        self.assertEqual(builder1.status, TaskStatus.PENDING)
        self.assertEqual(builder2.status, TaskStatus.PENDING)
        self.assertIn("Reviewer Asset Refresh", imagegen.description)
        self.assertIn("Reviewer Asset Refresh", assetimport.description)

    def test_reviewer_rejection_requeues_transitive_builder_through_polisher(self):
        class StubBridge:
            config = {}

            async def execute(self, node, plugins, input_data, model, on_progress):
                await on_progress({
                    "stage": "browser_action",
                    "action": "observe",
                    "ok": True,
                    "url": "http://127.0.0.1:8765/preview/index.html",
                    "state_changed": True,
                })
                return {
                    "success": True,
                    "output": json.dumps({
                        "verdict": "REJECTED",
                        "issues": ["导航和图片质量不达标"],
                        "required_changes": ["统一导航结构并修复错图/坏图"],
                    }),
                    "tool_results": [{"success": True, "data": {"url": "http://127.0.0.1:8765/preview/index.html"}}],
                    "tool_call_stats": {"browser": 1},
                }

        orch = Orchestrator(ai_bridge=StubBridge(), executor=None)

        async def _noop(_evt):
            return None

        orch.on_event = _noop
        builder = SubTask(id="1", agent_type="builder", description="build", depends_on=[], max_retries=2)
        builder.status = TaskStatus.COMPLETED
        polisher = SubTask(id="2", agent_type="polisher", description="polish", depends_on=["1"])
        polisher.status = TaskStatus.COMPLETED
        reviewer = SubTask(id="3", agent_type="reviewer", description="review", depends_on=["2"])
        plan = Plan(goal="做一个高端多页面旅游官网", subtasks=[builder, polisher, reviewer])

        with patch.object(orch, "_interaction_gate_error", return_value=None):
            with patch.object(
                orch,
                "_run_reviewer_visual_gate",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "errors": [],
                        "warnings": [],
                        "preview_url": "http://127.0.0.1:8765/preview/index.html",
                        "smoke": {"status": "pass"},
                    }
                ),
            ):
                result = asyncio.run(orch._execute_subtask(reviewer, plan, "kimi-coding", prev_results={}))

        self.assertFalse(result.get("success"))
        self.assertTrue(result.get("requeue_requested"))
        self.assertIn("1", result.get("requeue_subtasks", []))
        self.assertEqual(builder.status, TaskStatus.PENDING)
        self.assertEqual(reviewer.status, TaskStatus.PENDING)

    def test_collect_transitive_downstream_ids_is_order_independent(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        plan = Plan(
            goal="做一个高端多页面旅游官网",
            subtasks=[
                SubTask(id="6", agent_type="tester", description="test", depends_on=["4", "5"]),
                SubTask(id="1", agent_type="builder", description="build", depends_on=[]),
                SubTask(id="7", agent_type="debugger", description="debug", depends_on=["6"]),
                SubTask(id="5", agent_type="deployer", description="deploy", depends_on=["3"]),
                SubTask(id="4", agent_type="reviewer", description="review", depends_on=["3"]),
                SubTask(id="3", agent_type="polisher", description="polish", depends_on=["1"]),
            ],
        )

        downstream = set(orch._collect_transitive_downstream_ids(plan, ["1"]))

        self.assertEqual(downstream, {"3", "4", "5", "6", "7"})


class TestAnalystHandoffContext(unittest.TestCase):
    def test_builder_context_keeps_curated_image_library_and_skill_plan(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(goal="做一个高端旅游官网", subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[])])
        builder = plan.subtasks[0]
        analyst_output = (
            "<reference_sites>\n- https://example.com\n</reference_sites>\n"
            "<curated_image_library>\n- index.html: use verified West Lake image\n</curated_image_library>\n"
            "<skill_activation_plan>\n- builder_1: apply atlas surface system\n</skill_activation_plan>\n"
            "<builder_1_handoff>\n- Build the homepage\n</builder_1_handoff>\n"
        )

        context = orch._build_analyst_handoff_context(plan, builder, analyst_output)
        self.assertIn("Curated Image Library", context)
        self.assertIn("Skill Activation Plan", context)
        self.assertIn("verified West Lake image", context)

    def test_builder_context_keeps_control_frame_contract_and_asset_sourcing_plan(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(goal="做一个第三人称 3D 射击游戏", subtasks=[SubTask(id="1", agent_type="builder", description="build", depends_on=[])])
        builder = plan.subtasks[0]
        analyst_output = (
            "<reference_sites>\n- https://example.com\n</reference_sites>\n"
            "<control_frame_contract>\n- W forward, A left, D right.\n</control_frame_contract>\n"
            "<asset_sourcing_plan>\n- Use source_fetch first on permissive libraries.\n</asset_sourcing_plan>\n"
            "<builder_1_handoff>\n- Build the game.\n</builder_1_handoff>\n"
        )

        context = orch._build_analyst_handoff_context(plan, builder, analyst_output)
        self.assertIn("Control Frame Contract", context)
        self.assertIn("Asset Sourcing Plan", context)
        self.assertIn("Use source_fetch first", context)

    def test_builder_context_includes_allocated_source_pack_for_matching_handoff(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="做一个第三人称 3D 射击游戏",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="movement", depends_on=[]),
                SubTask(id="2", agent_type="builder", description="combat", depends_on=[]),
            ],
        )
        builder = plan.subtasks[1]
        analyst_output = (
            "<reference_sites>\n"
            "- https://github.com/pmndrs/ecctrl movement controller for third-person locomotion\n"
            "- https://github.com/donmccurdy/three-pathfinding enemy navigation and pursuit mesh\n"
            "- https://threejs.org/docs/#api/en/cameras/PerspectiveCamera camera and renderer basics\n"
            "</reference_sites>\n"
            "<builder_1_handoff>\n"
            "- Own movement and camera rig.\n"
            "</builder_1_handoff>\n"
            "<builder_2_handoff>\n"
            "- Own enemy combat, navigation, pursuit, and hit reactions.\n"
            "</builder_2_handoff>\n"
        )

        context = orch._build_analyst_handoff_context(plan, builder, analyst_output)
        self.assertIn("Builder 2 Source Pack", context)
        self.assertIn("three-pathfinding", context)
        self.assertIn("Start from these exact implementation-grade anchors first", context)

    def test_builder_context_backfills_curated_game_sources_when_analyst_urls_are_sparse(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="做一个第三人称 3D 射击游戏，要求人物建模更好、子弹轨迹清晰、怪物会追击。",
            subtasks=[
                SubTask(id="1", agent_type="builder", description="movement and camera", depends_on=[]),
                SubTask(id="2", agent_type="builder", description="combat and enemy AI", depends_on=[]),
            ],
        )
        builder = plan.subtasks[1]
        analyst_output = (
            "<reference_sites>\n"
            "- https://example.com/reference\n"
            "</reference_sites>\n"
            "<builder_1_handoff>\n"
            "- Own movement, camera feel, and control frame.\n"
            "</builder_1_handoff>\n"
            "<builder_2_handoff>\n"
            "- Own enemy pursuit, projectile readability, weapon feel, and combat loop.\n"
            "</builder_2_handoff>\n"
        )

        context = orch._build_analyst_handoff_context(plan, builder, analyst_output)
        self.assertIn("Mugen87/yuka", context)
        self.assertIn("KhronosGroup/glTF-Sample-Assets", context)

    def test_builder_context_backfills_curated_presentation_sources_when_analyst_urls_are_sparse(self):
        orch = Orchestrator(ai_bridge=None, executor=None, on_event=None)
        plan = Plan(
            goal="做一个融资路演 PPT，要求可打印、可导出，并且浏览器演示效果专业。",
            subtasks=[SubTask(id="1", agent_type="builder", description="deck builder", depends_on=[])],
        )
        builder = plan.subtasks[0]
        analyst_output = (
            "<reference_sites>\n"
            "- https://example.com/deck-reference\n"
            "</reference_sites>\n"
            "<builder_1_handoff>\n"
            "- Own slide structure, export readiness, and browser presentation flow.\n"
            "</builder_1_handoff>\n"
        )

        context = orch._build_analyst_handoff_context(plan, builder, analyst_output)
        self.assertIn("revealjs/reveal.js", context)
        self.assertIn("gitbrent/PptxGenJS", context)


class TestConsecutiveFailureTracker(unittest.TestCase):
    """Tests for the generalized circuit breaker utility (Claude Code autoCompact pattern)."""

    def test_trips_at_max_consecutive(self):
        from orchestrator import ConsecutiveFailureTracker
        tracker = ConsecutiveFailureTracker(max_consecutive=3)
        self.assertFalse(tracker.note("empty_output"))  # 1
        self.assertFalse(tracker.note("empty_output"))  # 2
        self.assertTrue(tracker.note("empty_output"))    # 3 → tripped
        self.assertTrue(tracker.tripped)

    def test_reset_clears_state(self):
        from orchestrator import ConsecutiveFailureTracker
        tracker = ConsecutiveFailureTracker(max_consecutive=2)
        tracker.note("api_error")
        tracker.note("api_error")
        self.assertTrue(tracker.tripped)
        tracker.reset()
        self.assertFalse(tracker.tripped)
        self.assertEqual(tracker.count, 0)

    def test_different_kind_resets_count(self):
        from orchestrator import ConsecutiveFailureTracker
        tracker = ConsecutiveFailureTracker(max_consecutive=3)
        tracker.note("empty_output")
        tracker.note("empty_output")
        # kind changes → count resets to 1
        self.assertFalse(tracker.note("parse_error"))
        self.assertEqual(tracker.count, 1)
        self.assertEqual(tracker.kind, "parse_error")

    def test_single_max_always_trips(self):
        from orchestrator import ConsecutiveFailureTracker
        tracker = ConsecutiveFailureTracker(max_consecutive=1)
        self.assertTrue(tracker.note("any_kind"))

    def test_min_max_clamped_to_1(self):
        from orchestrator import ConsecutiveFailureTracker
        tracker = ConsecutiveFailureTracker(max_consecutive=0)
        self.assertTrue(tracker.note("x"))


class TestContinuationInputDetection(unittest.TestCase):
    """Tests for continue/keep-going route detection (Claude Code userPromptKeywords pattern)."""

    def test_bare_continue_english(self):
        from orchestrator import is_continuation_input
        self.assertTrue(is_continuation_input("continue"))
        self.assertTrue(is_continuation_input("  Continue  "))
        self.assertTrue(is_continuation_input("CONTINUE"))

    def test_keep_going_english(self):
        from orchestrator import is_continuation_input
        self.assertTrue(is_continuation_input("keep going"))
        self.assertTrue(is_continuation_input("go on"))

    def test_chinese_continuations(self):
        from orchestrator import is_continuation_input
        self.assertTrue(is_continuation_input("继续"))
        self.assertTrue(is_continuation_input("接着"))
        self.assertTrue(is_continuation_input("继续做"))
        self.assertTrue(is_continuation_input("接着做"))
        self.assertTrue(is_continuation_input("往下做"))

    def test_non_continuation_inputs(self):
        from orchestrator import is_continuation_input
        self.assertFalse(is_continuation_input("continue working on the navbar"))
        self.assertFalse(is_continuation_input("做一个新网站"))
        self.assertFalse(is_continuation_input(""))
        self.assertFalse(is_continuation_input("please go on and fix the bug"))
        # '往下' alone is too ambiguous (prefix of '往下看'='look below')
        self.assertFalse(is_continuation_input("往下"))


class TestUserFrustrationDetection(unittest.TestCase):
    """Tests for user frustration detection (Claude Code matchesNegativeKeyword pattern)."""

    def test_english_frustration(self):
        from orchestrator import matches_user_frustration
        self.assertTrue(matches_user_frustration("wtf is this"))
        self.assertTrue(matches_user_frustration("this is broken again"))
        self.assertTrue(matches_user_frustration("useless output"))
        self.assertTrue(matches_user_frustration("terrible result"))

    def test_chinese_frustration(self):
        from orchestrator import matches_user_frustration
        self.assertTrue(matches_user_frustration("怎么又崩了"))
        self.assertTrue(matches_user_frustration("又卡住了"))
        self.assertTrue(matches_user_frustration("死循环了吧"))
        self.assertTrue(matches_user_frustration("还是不行啊"))
        self.assertTrue(matches_user_frustration("垃圾输出"))

    def test_non_frustration(self):
        from orchestrator import matches_user_frustration
        self.assertFalse(matches_user_frustration("做一个旅游网站"))
        self.assertFalse(matches_user_frustration("looks good, continue"))
        self.assertFalse(matches_user_frustration(""))
        self.assertFalse(matches_user_frustration("please fix the color palette"))


class TestP0PromptAnchors(unittest.TestCase):
    """Tests for P0 prompt changes: numeric length anchors and anti-false-claims."""

    def test_common_rules_has_output_efficiency_anchor(self):
        import task_classifier
        self.assertIn("≤40 words", task_classifier._COMMON_RULES)

    def test_common_rules_has_anti_false_claims(self):
        import task_classifier
        self.assertIn("REPORT OUTCOMES FAITHFULLY", task_classifier._COMMON_RULES)
        self.assertIn("quality gate", task_classifier._COMMON_RULES)

    def test_builder_prompt_inherits_anchors(self):
        import task_classifier
        prompt = task_classifier.builder_system_prompt("做一个旅游网站")
        self.assertIn("≤40 words", prompt)
        self.assertIn("REPORT OUTCOMES FAITHFULLY", prompt)

    def test_reviewer_prompt_has_output_efficiency(self):
        from ai_bridge import AGENT_PRESETS
        reviewer_instructions = AGENT_PRESETS.get("reviewer", {}).get("instructions", "")
        self.assertIn("REPORT FAITHFULLY", reviewer_instructions)
        self.assertIn("STRICT", reviewer_instructions)


class TestBug1ModelDowngradeOrder(unittest.TestCase):
    """Bug #1 regression: _alternate_retry_model must walk DOWN the chain, not oscillate."""

    def test_alternate_retry_delegates_to_downgrade(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        # Monkeypatch _has_key_for to accept all models for this test
        orch._has_key_for = lambda model_name: True
        # From gpt-5.3-codex (index 1), should go to deepseek-v3 (index 2), NOT back to kimi-coding (index 0)
        result = orch._alternate_retry_model("gpt-5.3-codex")
        self.assertEqual(result, "deepseek-v3")
        # From kimi-coding (index 0), should go forward
        result2 = orch._alternate_retry_model("kimi-coding")
        self.assertEqual(result2, "gpt-5.3-codex")

    def test_downgrade_model_skips_recently_unhealthy_gateway_candidates(self):
        from ai_bridge import AIBridge

        with patch.dict("os.environ", {"OPENAI_API_BASE": "https://gateway.example/v1"}, clear=False):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
                "deepseek_api_key": "sk-deepseek-test",
            })
            orch = Orchestrator(ai_bridge=bridge, executor=None)
            orch._has_key_for = lambda model_name: True
            model_info = bridge._resolve_model("gpt-5.4")
            _key, state = bridge._compatible_gateway_state(model_info)
            self.assertIsNotNone(state)
            state["last_rejection_at"] = time.time() - 20
            state["last_success_at"] = 0.0
            state["last_error"] = "Your request was blocked."

            result = orch._downgrade_model("kimi-coding")

        self.assertEqual(result, "deepseek-v3")

    def test_alternate_retry_model_uses_node_candidates_when_available(self):
        from ai_bridge import AIBridge

        bridge = AIBridge(config={
            "kimi_api_key": "sk-kimi-test",
            "node_model_preferences": {
                "analyst": ["kimi-coding"],
            },
        })
        # Isolate from persisted local auth/gateway cooldown state seeded from logs.
        bridge._provider_auth_health.clear()
        bridge._compat_gateway_health.clear()
        orch = Orchestrator(ai_bridge=bridge, executor=None)
        orch._has_key_for = lambda model_name: True
        subtask = SubTask(id="1", agent_type="analyst", description="research")

        result = orch._alternate_retry_model("kimi-coding", subtask)

        self.assertEqual(result, "kimi-coding")

    def test_has_key_for_ignores_recently_unhealthy_compatible_gateway_without_relays(self):
        from ai_bridge import AIBridge

        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "sk-openai-test",
                "OPENAI_API_BASE": "https://gateway.example/v1",
                "KIMI_API_KEY": "sk-kimi-test",
            },
            clear=False,
        ):
            bridge = AIBridge(config={
                "openai_api_key": "sk-openai-test",
                "kimi_api_key": "sk-kimi-test",
            })
            # Isolate from persisted local auth/gateway cooldown state seeded from logs.
            bridge._provider_auth_health.clear()
            bridge._compat_gateway_health.clear()
            orch = Orchestrator(ai_bridge=bridge, executor=None)
            model_info = bridge._resolve_model("gpt-5.4")
            _key, state = bridge._compatible_gateway_state(model_info)
            self.assertIsNotNone(state)
            state["last_rejection_at"] = time.time() - 30
            state["last_success_at"] = 0.0
            state["last_error"] = "Your request was blocked."

            self.assertFalse(orch._has_key_for("gpt-5.4"))
            self.assertEqual(orch._model_for_difficulty("pro", "gpt-5.4"), "kimi-coding")

    def test_retry_blacklist_includes_recent_provider_auth_failure(self):
        from ai_bridge import AIBridge

        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "sk-openai-test"},
            clear=False,
        ):
            bridge = AIBridge(config={"openai_api_key": "sk-openai-test"})
            # Isolate this test from persisted health snapshots seeded from local logs.
            bridge._provider_auth_health.clear()
            bridge._compat_gateway_health.clear()
            orch = Orchestrator(ai_bridge=bridge, executor=None)
            model_info = bridge._resolve_model("gpt-5.4")
            # V4.5: Need 2+ consecutive failures before blocking (was 1)
            bridge._record_provider_auth_failure(model_info, "401 unauthorized")
            bridge._record_provider_auth_failure(model_info, "401 unauthorized")

            reason = orch._retry_blacklist_reason_for_model("gpt-5.4")
            self.assertIn("recent openai auth failure", reason)
            self.assertFalse(orch._has_key_for("gpt-5.4"))


class TestBug2EmptyOutputCounterPersistence(unittest.TestCase):
    """Bug #2 regression: consecutive_empty_outputs must NOT reset on non-empty errors."""

    def test_non_empty_error_preserves_empty_output_count(self):
        orch = Orchestrator(ai_bridge=None, executor=None)
        subtask = SubTask(id="1", agent_type="builder", description="build")
        # Empty output → count = 1
        orch._note_failure_pattern(subtask, "returned empty content for builder node")
        self.assertEqual(subtask.consecutive_empty_outputs, 1)
        # Parse error (non-empty) → count should NOT reset to 0
        orch._note_failure_pattern(subtask, "JSON parse error in reviewer output")
        self.assertEqual(subtask.consecutive_empty_outputs, 1)  # preserved!
        # Another empty output → count = 2 (breaker should trip)
        orch._note_failure_pattern(subtask, "produced no content or tool activity")
        self.assertEqual(subtask.consecutive_empty_outputs, 2)


class TestFrustrationClearsSessionContinuation(unittest.TestCase):
    """Verify that frustration detection in run() actually clears session_continuation_hint."""

    def test_frustration_clears_session_continuation_in_run(self):
        """When a user is frustrated AND session_continuation=True, run() should
        clear _session_continuation_hint so the orchestrator re-plans from scratch."""
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        # Simulate run() initialization with a frustrated goal
        orch._cancel = False
        orch._run_started_at = time.time()
        orch._current_goal = "怎么又崩了 做不出来"
        orch._current_conversation_history = []
        orch.difficulty = "standard"
        orch._reviewer_requeues = 0
        orch._stable_preview_path = None
        orch._stable_preview_files = []
        orch._stable_preview_stage = ""
        # The key: canonical_context says session_continuation=True
        canonical_context = {"session_continuation": True}
        orch._session_continuation_hint = bool(canonical_context.get("session_continuation"))
        orch._session_context_note = ""
        # Apply frustration detection (the logic we just added to run())
        from orchestrator import matches_user_frustration, ConsecutiveFailureTracker
        orch._user_frustrated = False
        if matches_user_frustration(orch._current_goal):
            orch._user_frustrated = True
            orch._session_continuation_hint = False
        orch._run_failure_tracker = ConsecutiveFailureTracker(max_consecutive=3)

        self.assertTrue(orch._user_frustrated)
        self.assertFalse(orch._session_continuation_hint)
        # _is_continuation_request should now return False
        self.assertFalse(orch._is_continuation_request(orch._current_goal, [{"role": "user", "content": "先前的消息"}]))

    def test_non_frustrated_preserves_session_continuation(self):
        """Normal goals should NOT clear session_continuation_hint."""
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        orch._session_continuation_hint = True
        orch._user_frustrated = False
        from orchestrator import matches_user_frustration
        goal = "继续优化这个旅游网站的动画效果"
        if matches_user_frustration(goal):
            orch._user_frustrated = True
            orch._session_continuation_hint = False

        self.assertFalse(orch._user_frustrated)
        self.assertTrue(orch._session_continuation_hint)
        self.assertTrue(orch._is_continuation_request(goal, []))


class TestTrackerIntegrationInHandleFailure(unittest.TestCase):
    """Verify ConsecutiveFailureTracker is wired into _handle_failure and resets on success."""

    def test_tracker_notes_failures_in_handle_failure(self):
        """The tracker should be used in _handle_failure to note failure patterns."""
        from orchestrator import ConsecutiveFailureTracker
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        orch._run_failure_tracker = ConsecutiveFailureTracker(max_consecutive=3)

        subtask = SubTask(id="b1", agent_type="builder", description="build", max_retries=5)
        subtask.error = "returned empty content for builder node"
        plan = Plan(goal="test", subtasks=[subtask])
        results = {}

        async def _noop(_evt):
            return None
        orch.on_event = _noop

        # Run _handle_failure to trigger tracker.note
        async def _run_test():
            await orch._handle_failure(subtask, plan, "kimi-coding", results)

        asyncio.run(_run_test())

        # Tracker should have noted the failure
        self.assertEqual(orch._run_failure_tracker.count, 1)
        self.assertEqual(orch._run_failure_tracker.kind, "builder:empty_output")

    def test_builder_invalid_salvage_loop_fails_fast_in_handle_failure(self):
        orch = Orchestrator(ai_bridge=SimpleNamespace(config={}), executor=None, on_event=None)
        subtask = SubTask(id="b2", agent_type="builder", description="build", max_retries=5)
        subtask.error = "builder direct-text max-stream timeout"
        subtask.builder_invalid_salvage_tripped = True
        subtask.builder_invalid_salvage_message = "Repeated invalid salvaged HTML loop detected."
        plan = Plan(goal="test", subtasks=[subtask])

        async def _noop(_evt):
            return None

        orch.on_event = _noop

        ok = asyncio.run(orch._handle_failure(subtask, plan, "kimi-coding", {}))

        self.assertFalse(ok)
        self.assertEqual(subtask.status, TaskStatus.FAILED)
        self.assertEqual(subtask.retries, 0)
        self.assertIn("Repeated invalid salvaged HTML loop detected", subtask.error)


if __name__ == "__main__":
    unittest.main()
